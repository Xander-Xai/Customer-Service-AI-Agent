"""多进程指标聚合契约（``app`` / ``worker`` 两个容器之间的可见性）。

这不是「锦上添花的可观测性」，而是 DLQ 告警**能不能真的触发**的前提。

断链的两半
----------
1. ``REGISTRY`` 从未被序列化（``/metrics`` 端点缺失）—— 任何指标都收不到。
2. 即使补上端点，``agent_run_dead_letter_total`` 仍然**恒为 0**：
   ``runtime/run_service.py::mark_dead_letter`` 在 **Celery worker 进程**里递增它，
   而 Prometheus 抓的是 **app 容器**。``prometheus_client`` 的默认 ``REGISTRY``
   是进程内的，两个容器各有一份，互不可见。

只修第 1 半会得到一个**更隐蔽**的故障：指标存在、target 是 UP、告警语法正确，
但值永远是 0 —— `increase(...[5m]) > 0` 永远为假。这比「指标压根不存在」更难在
评审或事故里被一眼看出。

因此本文件用**真实子进程**验证第 2 半：
一个子进程递增计数并退出，另一个**独立进程**读暴露面，必须看到该计数。
它不 mock ``prometheus_client``，用的是真实 mmap 文件 + 真实 ``MultiProcessCollector``。

同时验证「只聚合一次」：多进程模式下默认 ``REGISTRY`` 必须被排除在暴露之外，
否则同一批样本会被数两遍（这正是「重复累加」要防的错误）。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 子进程 A：像 worker 容器那样递增可靠性指标，然后退出。
_WRITER_SNIPPET = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {repo!r})
    from core.monitoring import agent_run_dead_letter_total, agent_run_retry_total
    for _ in range({increments}):
        agent_run_dead_letter_total.inc()
    for _ in range({retries}):
        agent_run_retry_total.inc()
    from prometheus_client import multiprocess as _mp
    _mp.mark_process_dead(os.getpid())
    print("written", flush=True)
    """
)

#: 子进程 B：像 app 容器那样**自己也写一点**再暴露（真实拓扑：API 进程既写缓存 /
#: 工具结果等指标，又是唯一被抓取的那个进程）。"也写" 是关键 —— 见下方双算用例。
_READER_SNIPPET = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {repo!r})
    from core.monitoring import agent_run_dead_letter_total as _dl
    for _ in range({own_increments}):
        _dl.inc()
    from core.metrics_exposition import exposition_text
    sys.stdout.buffer.write(exposition_text())
    """
)


def _run_snippet(snippet: str, multiprocess_dir: Path) -> bytes:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "PROMETHEUS_MULTIPROC_DIR": str(multiprocess_dir),
        # 明确关掉可能从父环境继承的东西，保证子进程语义干净可复现
        "DEV_MODE": "true",
    }
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        timeout=180,
    )
    assert result.returncode == 0, f"子进程失败:\nstdout={result.stdout}\nstderr={result.stderr}"
    return result.stdout.encode()


def _sample_lines(body: bytes, metric: str) -> list[float]:
    """返回该 metric 的全部**样本行**的值（同名多行 = 暴露面重复，不是取值问题）。"""
    values: list[float] = []
    for line in body.decode("utf-8", "replace").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if line.split("{", 1)[0].split(" ", 1)[0].strip() == metric:
            values.append(float(line.rsplit(" ", 1)[1]))
    return values


def _sample(body: bytes, metric: str) -> float | None:
    """取该 metric 的唯一样本值；缺样本返回 None，**多份样本返回最后一份**。

    多份样本本身由 :func:`test_exposition_emits_each_metric_exactly_once` 单独断言；
    这里取最后一份是为了让失败信息反映「Prometheus 实际会采到的那个值」
    （text exposition 同名重复时解析器取最后一个），而不是任意一份。
    """
    values = _sample_lines(body, metric)
    return values[-1] if values else None


@pytest.mark.unit
def test_worker_process_increments_are_visible_to_a_separate_exposition_process(tmp_path):
    """worker 进程写的计数，app 侧的暴露进程必须看得到（这决定 DLQ 告警能否触发）。"""
    mp_dir = tmp_path / "prom-metrics"
    mp_dir.mkdir()

    _run_snippet(
        _WRITER_SNIPPET.format(repo=str(ROOT), increments=7, retries=3),
        mp_dir,
    )
    body = _run_snippet(_READER_SNIPPET.format(repo=str(ROOT), own_increments=0), mp_dir)

    assert _sample(body, "agent_run_dead_letter_total") == 7.0, (
        "另一个进程看不到 worker 递增的 agent_run_dead_letter_total —— "
        "DLQ 告警仍然不会触发（指标存在但恒为 0）"
    )
    assert _sample(body, "agent_run_retry_total") == 3.0


@pytest.mark.unit
def test_multiprocess_exposition_does_not_double_count(tmp_path):
    """多进程模式只聚合一次，不把同一批样本数两遍。

    正确实现是「新建 registry + MultiProcessCollector，**排除**默认 REGISTRY」。
    若把默认 REGISTRY 也挂上去，同一个 metric 会输出**两条样本行**：

        agent_run_dead_letter_total 7.0     <- MultiProcessCollector（mmap 聚合，正确）
        agent_run_dead_letter_total 2.0     <- 默认 REGISTRY（本进程局部值，重复）

    这不是「数值翻倍」而是**非法暴露**：Prometheus 的 text parser 遇到同名同
    label 的重复样本会报 duplicate sample，最终只保留其中一个 —— 也就是说被保留的
    那个是「app 进程自己写的数」，worker 的计数照样丢掉，DLQ 告警仍然不会触发。
    而且它**不会让进程崩溃**，所以这类错误在测试缺失时是彻底静默的。

    为什么读侧也要递增
    ------------------
    ``prometheus_client`` 的多进程模式下，指标在**写入进程内部读回是 0**
    （值只存在于 mmap 文件里）。只有当暴露进程自己写过该指标时，把它额外挂进暴露面
    才会显出重复行。而真实拓扑恰恰如此：``app`` 进程既写缓存 / 工具结果 / HITL
    审批等指标，又是唯一被抓取的那个进程。因此读侧必须也递增，否则这个用例测的是
    「恰好不会重复」，而不是「实现不会重复」。
    """
    mp_dir = tmp_path / "prom-metrics"
    mp_dir.mkdir()
    _run_snippet(_WRITER_SNIPPET.format(repo=str(ROOT), increments=5, retries=0), mp_dir)
    body = _run_snippet(_READER_SNIPPET.format(repo=str(ROOT), own_increments=2), mp_dir)

    values = _sample_lines(body, "agent_run_dead_letter_total")
    assert len(values) == 1, f"同一指标暴露了多份样本（Prometheus 会丢弃/报错）：{values}"
    assert values[0] == 7.0, f"聚合值不对：期望 5(worker) + 2(app) = 7.0，实际 {values[0]}"


@pytest.mark.unit
def test_exposition_emits_each_series_exactly_once(tmp_path):
    """暴露面里不得有任何**同名同 label 集**的重复样本（覆盖全部 family，不只 DLQ）。

    正确的多重聚合不变量不是「每个指标名只出现一次」—— 直方图的 ``_bucket`` 会按
    ``le`` 合法地出现多次；而是「**每条时序（指标名 + label 集）只出现一次**」。
    重复时序在 Prometheus text parser 里是 duplicate sample，会被拒绝或只保留最后
    一个，且**不会**让进程崩溃 —— 所以没有这条断言时，它是彻底静默的。
    """
    mp_dir = tmp_path / "prom-metrics"
    mp_dir.mkdir()
    _run_snippet(_WRITER_SNIPPET.format(repo=str(ROOT), increments=3, retries=1), mp_dir)
    body = _run_snippet(_READER_SNIPPET.format(repo=str(ROOT), own_increments=1), mp_dir)

    seen: dict[str, int] = {}
    for line in body.decode("utf-8", "replace").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        series = line.split(" ", 1)[0].strip()  # 含 `{...}` 的完整时序标识
        seen[series] = seen.get(series, 0) + 1
    duplicated = {series: n for series, n in seen.items() if n > 1}
    assert not duplicated, f"这些时序在暴露面里出现了多份样本：{duplicated}"


@pytest.mark.unit
def test_multiprocess_mode_is_reported_in_the_exposition(tmp_path):
    """暴露面自述自己是否处于多进程聚合模式（运维不靠读代码即可判断）。"""
    mp_dir = tmp_path / "prom-metrics"
    mp_dir.mkdir()
    _run_snippet(_WRITER_SNIPPET.format(repo=str(ROOT), increments=1, retries=0), mp_dir)
    body = _run_snippet(_READER_SNIPPET.format(repo=str(ROOT), own_increments=0), mp_dir)
    assert _sample(body, "prometheus_multiprocess_enabled") == 1.0

    # 单进程（当前 pytest 进程）必须如实报告 0 —— 这是「没开聚合」的可见事实，
    # 而不是一句文档。运维可以直接对它配告警。
    from core.metrics_exposition import exposition_text

    assert _sample(exposition_text(), "prometheus_multiprocess_enabled") == 0.0


@pytest.mark.unit
def test_single_process_exposition_still_exposes_the_dead_letter_counter():
    """单进程（默认/开发/测试）不得因多进程支持而退化。"""
    from core.metrics_exposition import exposition_text

    body = exposition_text()
    assert b"# TYPE agent_run_dead_letter_total counter" in body
    assert (
        _sample(body, "prometheus_exposition_metric_families") > 50
    ), "暴露的 family 数异常少，指标可能没有真正注册到暴露面上"


@pytest.mark.unit
def test_wipe_multiprocess_dir_is_explicit_and_only_touches_db_files(tmp_path, monkeypatch):
    """清理只由「服务启动」显式调用；且只删 .db，绝不误删目录里的其它内容。"""
    from core.metrics_exposition import wipe_multiprocess_dir

    mp_dir = tmp_path / "prom-metrics"
    mp_dir.mkdir()
    (mp_dir / "counter_1.db").write_bytes(b"x")
    (mp_dir / "keepme.txt").write_text("do not delete")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(mp_dir))

    removed = wipe_multiprocess_dir()
    assert removed == 1
    assert not (mp_dir / "counter_1.db").exists()
    assert (mp_dir / "keepme.txt").exists(), "清理函数删掉了非指标文件"


@pytest.mark.unit
def test_wipe_is_a_noop_when_multiprocess_mode_is_off(monkeypatch):
    """未启用多进程时清理必须是 no-op —— 否则会误删一个根本不存在的部署目录。"""
    from core.metrics_exposition import wipe_multiprocess_dir

    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    assert wipe_multiprocess_dir() == 0
