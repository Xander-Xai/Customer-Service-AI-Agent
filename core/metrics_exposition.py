"""Prometheus 暴露面：单进程 / 多进程（multi-process）注册表的选择。

为什么需要这个模块
------------------
暴露 ``REGISTRY`` 本身不够。本服务的指标**在多个进程里被写**：

- ``app`` 容器：HTTP 快路径、缓存、RAG、MCP、``csai_*`` 业务聚合；
- ``worker`` 容器（**另一个容器**，另一套进程）：``runtime/run_service.py``
  在 ``mark_dead_letter`` / ``mark_retrying`` / heartbeat 等路径上调
  ``runtime/metrics.py`` 的转发函数，最终落到 ``core/monitoring.py`` 注册的
  ``prometheus_client`` 计数器 —— 包括 DLQ 闭环依赖的
  ``agent_run_dead_letter_total``；
- gunicorn 的多个 API 子进程也各写各的。

``prometheus_client`` 的默认 ``REGISTRY`` 是**进程内**的。于是：

    即使把 REGISTRY 完整暴露出来，Prometheus 抓到的 ``app`` 容器里
    ``agent_run_dead_letter_total`` 也**永远是 0** —— 递增发生在 worker 容器，
    Prometheus 抓不到那个进程。告警因此仍然不会触发，而且这次是「看起来正常」地
    不触发（指标存在、值恒为 0），比「指标压根不存在」更难被发现。

解法是 ``prometheus_client`` 官方提供的多进程模式
（``PROMETHEUS_MULTIPROC_DIR`` + ``MultiProcessCollector``）：所有进程把样本写到
共享目录里的 mmap 文件，由暴露侧聚合成一条时间序列。

本模块的职责边界
----------------
**只做注册表选择**，不新增指标、不累加、不改语义：

- 未启用多进程（``PROMETHEUS_MULTIPROC_DIR`` 未设置）→ 暴露默认 ``REGISTRY``；
  这是绝大多数测试与单机开发的路径，行为与引入本模块之前完全一致。
- 已启用 → 用 ``CollectorRegistry`` + ``MultiProcessCollector`` 聚合**全部进程**
  的样本。**只此一次聚合**：默认 ``REGISTRY`` 的 collector 必须被排除，否则同一
  批样本会被数两遍（``core/monitoring.py`` 里那些 counter 就会翻倍）—— 这正是
  「重复累加」要防的那类错误。

多进程模式下的 gauge 语义由**各指标自己的 ``multiprocess_mode``** 决定
（见 ``core/monitoring.py`` 中的声明）。默认是 ``all``（跨进程求和），对 counter /
histogram 是正确的，对「比值型」gauge 会把 N 个副本的同一个比值加起来。相关 gauge
已显式声明 ``mostrecent`` / ``livesum``，不要靠「默认值也差不多」蒙混。
"""

from __future__ import annotations

import os

from core.logger import get_logger

logger = get_logger("core.metrics_exposition")

#: 官方多进程模式的环境变量。必须在 ``prometheus_client`` 被 import **之前**设置，
#: 否则该库不会切换到多进程写入路径。
MULTIPROC_ENV_VAR = "PROMETHEUS_MULTIPROC_DIR"


def multiprocess_enabled() -> bool:
    """当前进程是否运行在 ``prometheus_client`` 多进程模式。

    只看环境变量：``prometheus_client`` 自己就是用它决定是否走 mmap 写入的
    （见 ``prometheus_client.values.ValueClass``），本函数与之保持同一判据，
    避免出现「库认为开了、我们认为没开」的分裂。
    """
    return bool(os.environ.get(MULTIPROC_ENV_VAR, "").strip())


def _build_multiprocess_registry():
    """构造聚合所有进程样本的 ``CollectorRegistry``（仅多进程模式调用）。"""
    from prometheus_client import CollectorRegistry
    from prometheus_client.multiprocess import MultiProcessCollector

    registry = CollectorRegistry()
    MultiProcessCollector(registry, path=os.environ[MULTIPROC_ENV_VAR])
    return registry


def exposition_registry():
    """返回本次暴露应当使用的 registry。

    - 多进程模式：新建的聚合 registry（**不含**默认 registry，避免双算）。
    - 单进程：默认 ``REGISTRY``。
    """
    if multiprocess_enabled():
        return _build_multiprocess_registry()
    from prometheus_client import REGISTRY

    return REGISTRY


def exposition_text() -> bytes:
    """Prometheus 文本格式的完整暴露内容（与 ``/metrics`` 路由共用同一入口）。"""
    from prometheus_client import generate_latest

    registry = exposition_registry()
    # 先把「本次暴露覆盖了多少 family / 是否多进程聚合」写进 gauge，再序列化，
    # 这样这两个事实一定出现在同一份 payload 里（顺序反了就会差一个自指计数）。
    _annotate_exposition(registry)
    return generate_latest(registry)


def _annotate_exposition(registry) -> None:
    """在暴露内容里追加「本次暴露覆盖了多少 family / 是否多进程聚合」。

    让指标**自己**回答一个运维必须能一眼看出、而不该靠读代码才回答的问题：
    「我看到的指标，是全部进程的，还是只有被抓取的那个进程的？」

    背景：``agent_run_dead_letter_total`` 等可靠性指标由 **worker 容器**递增
    （``runtime/run_service.py`` → ``runtime/metrics.py`` → ``core.monitoring``），
    而 Prometheus 抓的是 **app 容器**。``prometheus_client`` 的默认 REGISTRY 是
    进程内的，因此若未启用多进程模式，该指标在抓取侧**恒为 0** —— 它存在、它被
    scrape、它就是不动。这种「看起来正常地不触发」比「指标缺失」更难发现，
    所以把它做成可查询的事实而不是文档里的一句话。

    家族计数取自 registry 自身的 ``collect()``，即「**真的会被输出**的 family 数」，
    而不是「代码里声明了多少」—— 后者正是本次断链的原始形态（注册了却没暴露）。

    **顺序至关重要**：必须先 import ``core.monitoring``（它在 import 期完成注册）
    再数家族。反过来做会数到「import 之前」的快照，于是这个自描述指标会一边报着
    10 个家族、一边输出 136 个 —— 一个自己打自己脸的数字比没有还糟。
    """
    try:
        from core.monitoring import (  # noqa: F401  (import 即注册)
            prometheus_exposition_metric_families,
            prometheus_multiprocess_enabled,
        )
    except Exception:  # noqa: BLE001 - 自描述失败不得影响暴露
        return
    try:
        families = len({metric.name for metric in registry.collect()})
    except Exception:  # noqa: BLE001 - 自描述失败不得影响暴露
        return
    prometheus_multiprocess_enabled.set(1 if multiprocess_enabled() else 0)
    prometheus_exposition_metric_families.set(families)


def wipe_multiprocess_dir() -> int:
    """清空多进程样本目录，返回删除的文件数。

    **只能由「服务启动」调用一次**，绝不能每个进程启动都调：否则先起来的 worker
    的样本会被后起来的 API 进程清掉，指标静默归零。真正的危害是它会把
    「重启后计数归零」伪装成「这段时间没有事件」，让 DLQ 告警在部署窗口里失明。

    因此本函数只提供能力，不在任何 import 期自动调用 —— 调用点必须是显式的
    运维步骤（见 compose 的 entrypoint 步骤与部署文档）。
    """
    directory = os.environ.get(MULTIPROC_ENV_VAR, "").strip()
    if not directory or not os.path.isdir(directory):
        return 0
    removed = 0
    for entry in os.listdir(directory):
        if not entry.endswith(".db"):
            continue
        try:
            os.unlink(os.path.join(directory, entry))
            removed += 1
        except OSError as exc:  # pragma: no cover - 权限/竞态
            logger.warning("清理多进程指标文件失败 %s: %s", entry, exc)
    logger.info("已清理多进程指标目录 %s（%d 个文件）", directory, removed)
    return removed


__all__ = [
    "MULTIPROC_ENV_VAR",
    "exposition_registry",
    "exposition_text",
    "multiprocess_enabled",
    "wipe_multiprocess_dir",
]


def _main() -> int:
    """``python -m core.metrics_exposition`` —— 供服务启动时清理样本目录。

    **只能在「整个服务栈重启」时执行一次**。每个进程启动都执行是错的：先起来的
    worker 写好的样本会被后起来的 app 进程清掉，指标静默归零 —— 部署窗口内 DLQ
    告警会失明，而没有任何报错。compose 用一次性 ``metrics-init`` 服务保证它只跑一次。
    """
    if not multiprocess_enabled():
        print(
            f"{MULTIPROC_ENV_VAR} 未设置：当前为单进程模式，无需清理（no-op）。",
        )
        return 0
    removed = wipe_multiprocess_dir()
    print(f"✅ 已清理多进程指标样本：{removed} 个文件")
    return 0


if __name__ == "__main__":  # pragma: no cover - 运维入口
    raise SystemExit(_main())
