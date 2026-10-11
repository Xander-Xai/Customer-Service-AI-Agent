"""跨进程 Prometheus 指标拓扑契约（``app`` / ``worker`` / Prometheus 三方）。

为什么这些是「契约」而不是「配置建议」
--------------------------------------
``agent_run_dead_letter_total``（DLQ 告警链的**唯一**数据源）由 **worker 容器**
递增，而 Prometheus 抓的是 **app 容器**。``prometheus_client`` 的默认 REGISTRY 是
**进程内**的，因此要让它真正被抓到，必须同时满足三条：

1. 两个容器指向**同一个** ``PROMETHEUS_MULTIPROC_DIR``（且是共享卷，不是各自本地目录）；
2. 该目录在服务栈启动时被**清理恰好一次**（多清会把已写入的样本抹掉，指标静默归零）；
3. 暴露侧用 ``MultiProcessCollector`` 聚合（``core/metrics_exposition.py``）。

少任何一条，指标都是「存在、target 是 UP、值不动」—— 比「指标不存在」更难发现。
这里把三条锁住，防止后续有人只改了其中一半。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "compose" / "docker-compose.yml"

MULTIPROC_ENV_VAR = "PROMETHEUS_MULTIPROC_DIR"


def _services() -> dict:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = data.get("services")
    assert isinstance(services, dict), "docker-compose.yml 缺少 services"
    return services


def _env_list(service: dict) -> list[str]:
    env = service.get("environment") or []
    if isinstance(env, dict):
        return [f"{k}={v}" for k, v in env.items()]
    return list(env)


def _env_value(service: dict, key: str) -> str | None:
    for item in _env_list(service):
        name, _, value = item.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


@pytest.mark.unit
def test_app_and_worker_share_one_multiprocess_metrics_path():
    """app 与 worker 必须指向**同一个**目录，否则 worker 的计数对抓取侧不可见。"""
    services = _services()
    app_path = _env_value(services["app"], MULTIPROC_ENV_VAR)
    worker_path = _env_value(services["worker"], MULTIPROC_ENV_VAR)
    assert app_path, f"app 未设置 {MULTIPROC_ENV_VAR}"
    assert worker_path, f"worker 未设置 {MULTIPROC_ENV_VAR}"
    assert app_path == worker_path, (
        f"app 与 worker 的 {MULTIPROC_ENV_VAR} 不同：{app_path!r} vs {worker_path!r} —— "
        "指标无法跨进程聚合"
    )


@pytest.mark.unit
def test_multiprocess_metrics_dir_is_a_shared_volume_not_container_local():
    """必须是**共享卷**。bind mount 到容器内路径也行，但不能让两个容器各写各的目录。"""
    services = _services()
    for name in ("app", "worker", "metrics-init"):
        volumes = [str(v) for v in (services[name].get("volumes") or [])]
        assert any(
            "/app/var/prom-metrics" in v for v in volumes
        ), f"{name} 没有挂载共享的多进程指标目录"

    declared = set(yaml.safe_load(COMPOSE.read_text(encoding="utf-8")).get("volumes") or {})
    assert "prom-metrics" in declared, "顶层 volumes 未声明 prom-metrics（会被当成容器本地目录）"


@pytest.mark.unit
def test_metrics_wipe_runs_exactly_once_not_per_process():
    """清理必须是一次性服务，**不能**出现在 app / worker 的启动命令里。

    多清一次 = 把先启动进程的样本抹掉 = 指标静默归零，部署窗口内 DLQ 告警失明且
    无任何报错。这类错误只在重启时发生，因此单靠运行期测试永远抓不到。
    """
    services = _services()
    init = services.get("metrics-init")
    assert init is not None, "缺少 metrics-init 一次性初始化服务"
    command = str(init.get("command", ""))
    assert "core.metrics_exposition" in command, f"metrics-init 未执行样本清理：{command!r}"
    assert init.get("restart", "") == "no", "metrics-init 必须跑完就退出，不能重启"

    for name in ("app", "worker"):
        service_command = str(services[name].get("command", ""))
        assert (
            "core.metrics_exposition" not in service_command
        ), f"{name} 的启动命令里带了样本清理 —— 每个进程都清一遍会把别人的样本抹掉"

    for name in ("app", "worker"):
        depends = services[name].get("depends_on") or {}
        assert isinstance(depends, dict) and depends.get("metrics-init") == {
            "condition": "service_completed_successfully"
        }, f"{name} 未等待 metrics-init 成功完成（清理与写入的时序不确定）"


@pytest.mark.unit
def test_only_app_needs_to_expose_metrics():
    """只有 app 提供 HTTP；worker 不应再起一个 HTTP 暴露面（那会造成两个 series）。

    真正的暴露聚合只在 ``core.metrics_exposition``；worker 侧不注册 HTTP server。
    """
    services = _services()
    worker_command = str(services["worker"].get("command", ""))
    assert "celery" in worker_command
    for forbidden in ("uvicorn", "gunicorn", "8000"):
        assert forbidden not in worker_command, f"worker 不应启动 HTTP 服务（出现 {forbidden}）"
