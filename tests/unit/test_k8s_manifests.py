"""Kubernetes 最小部署清单结构校验（不依赖集群 / kubectl）。

验证：API 与 Worker 独立 Deployment、探针、资源、滚动更新、状态外置（无本地
PVC/hostPath）、Secret 仅占位符、Ingress SSE/WS 注解、HPA、无过度平台化组件。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

K8S = Path(__file__).resolve().parents[2] / "deploy" / "k8s"

REQUIRED_FILES = [
    "namespace.yaml",
    "configmap.yaml",
    "secret.example.yaml",
    "api-deployment.yaml",
    "api-service.yaml",
    "worker-deployment.yaml",
    "ingress.yaml",
    "hpa-api.yaml",
    "hpa-worker.yaml",
    "README.md",
    "kustomization.yaml",
]


def _docs(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [d for d in yaml.safe_load_all(f) if isinstance(d, dict)]


def _one(path: Path, kind: str) -> dict:
    for doc in _docs(path):
        if doc.get("kind") == kind:
            return doc
    raise AssertionError(f"{path.name} 缺少 kind={kind}")


def _container(deploy: dict) -> dict:
    return deploy["spec"]["template"]["spec"]["containers"][0]


@pytest.mark.unit
def test_required_files_exist():
    for name in REQUIRED_FILES:
        assert (K8S / name).exists(), f"缺少 {name}"


@pytest.mark.unit
def test_all_manifests_parse():
    for path in K8S.glob("*.yaml"):
        docs = _docs(path)
        assert docs, f"{path.name} 无有效 YAML 文档"


@pytest.mark.unit
def test_api_and_worker_are_separate_deployments():
    api = _one(K8S / "api-deployment.yaml", "Deployment")
    worker = _one(K8S / "worker-deployment.yaml", "Deployment")
    assert api["metadata"]["name"] == "api"
    assert worker["metadata"]["name"] == "worker"
    api_cmd = " ".join(_container(api).get("command", []))
    worker_cmd = " ".join(_container(worker).get("command", []))
    assert "gunicorn" in api_cmd and "celery" not in api_cmd
    assert "celery" in worker_cmd and "gunicorn" not in worker_cmd
    # 单 Pod 不得同时启动 API + worker
    assert len(api["spec"]["template"]["spec"]["containers"]) == 1
    assert len(worker["spec"]["template"]["spec"]["containers"]) == 1


@pytest.mark.unit
def test_api_has_probes_resources_and_rolling_update():
    api = _one(K8S / "api-deployment.yaml", "Deployment")
    container = _container(api)
    assert container["readinessProbe"]["httpGet"]["path"] == "/api/health"
    assert container["livenessProbe"]["httpGet"]["path"] == "/api/health"
    assert container["resources"]["requests"] and container["resources"]["limits"]
    assert api["spec"]["strategy"]["type"] == "RollingUpdate"
    assert api["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0


@pytest.mark.unit
def test_worker_has_graceful_shutdown_and_probes():
    worker = _one(K8S / "worker-deployment.yaml", "Deployment")
    pod = worker["spec"]["template"]["spec"]
    assert pod["terminationGracePeriodSeconds"] >= 180  # > AGENT_RUN_TASK_TIME_LIMIT
    container = _container(worker)
    assert "readinessProbe" in container and "livenessProbe" in container
    assert container["resources"]["requests"] and container["resources"]["limits"]


@pytest.mark.unit
def test_no_local_persistent_state():
    for name in ("api-deployment.yaml", "worker-deployment.yaml"):
        text = (K8S / name).read_text(encoding="utf-8")
        assert "PersistentVolumeClaim" not in text
        assert "hostPath" not in text
        # 挂载的卷只能是 emptyDir（日志），不是状态
        for doc in _docs(K8S / name):
            if doc.get("kind") == "Deployment":
                volumes = doc["spec"]["template"]["spec"].get("volumes", [])
                for vol in volumes:
                    assert "emptyDir" in vol, f"{name} 使用了非 emptyDir 卷: {vol}"


@pytest.mark.unit
def test_configmap_has_state_externalization_and_decoupled_dispatch():
    cm = _one(K8S / "configmap.yaml", "ConfigMap")
    data = cm["data"]
    assert data["AGENT_RUN_DISPATCH"] == "celery"
    assert data["LANGGRAPH_CHECKPOINT_BACKEND"] == "postgres"
    assert data["SESSION_STORAGE_BACKEND"] == "redis"
    assert data["AGENT_RUN_THREAD_LOCK_BACKEND"] == "redis"
    assert data["RUN_EVENT_STREAM_BACKEND"] == "redis"
    assert data["QDRANT_HOST"]
    assert data["API_KEY_ENABLED"] == "true"


@pytest.mark.unit
def test_secret_example_only_placeholders():
    secret = _one(K8S / "secret.example.yaml", "Secret")
    assert secret["metadata"]["name"] == "customer-ai-secrets"
    values = secret["stringData"]
    assert values, "secret.example.yaml 无 stringData"
    for key, value in values.items():
        upper = str(value).upper()
        assert any(
            token in upper
            for token in ("PLACEHOLDER", "REPLACE_ME", "REPLACE-ME", "CHANGE_ME", "CHANGE-ME")
        ), f"{key} 不是占位符: {value!r}"
        # 绝不能出现真实密钥形态
        assert not re.search(r"sk-[A-Za-z0-9]{20,}", str(value))
        assert "BEGIN" not in str(value)


@pytest.mark.unit
def test_ingress_sse_and_websocket_annotations():
    ing = _one(K8S / "ingress.yaml", "Ingress")
    ann = ing["metadata"]["annotations"]
    assert ann["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"
    assert int(ann["nginx.ingress.kubernetes.io/proxy-read-timeout"]) >= 3600
    assert int(ann["nginx.ingress.kubernetes.io/proxy-send-timeout"]) >= 3600
    assert ann["nginx.ingress.kubernetes.io/proxy-http-version"] == "1.1"
    assert ann["nginx.ingress.kubernetes.io/websocket-services"] == "api"


@pytest.mark.unit
def test_hpa_targets_api_and_worker():
    api_hpa = _one(K8S / "hpa-api.yaml", "HorizontalPodAutoscaler")
    worker_hpa = _one(K8S / "hpa-worker.yaml", "HorizontalPodAutoscaler")
    assert api_hpa["spec"]["scaleTargetRef"]["name"] == "api"
    assert worker_hpa["spec"]["scaleTargetRef"]["name"] == "worker"
    assert api_hpa["spec"]["minReplicas"] >= 1 and worker_hpa["spec"]["minReplicas"] >= 1
    # worker custom-metric autoscaling 是后续项，需在文档中说明
    worker_text = (K8S / "hpa-worker.yaml").read_text(encoding="utf-8")
    assert "队列深度" in worker_text or "queue depth" in worker_text.lower()


@pytest.mark.unit
def test_kustomization_resources_exist():
    kustomization = _one(K8S / "kustomization.yaml", "Kustomization")
    for resource in kustomization["resources"]:
        assert (K8S / resource).exists(), f"kustomization 引用缺失文件: {resource}"
    # 不应包含 secret.example（避免误 apply 占位符）
    assert "secret.example.yaml" not in kustomization["resources"]


@pytest.mark.unit
def test_no_over_platformization():
    forbidden = ("istio", "argocd", "kafka", "temporal", "service mesh", "operator")
    for path in K8S.rglob("*.yaml"):
        text = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"{path.name} 引入了禁止的平台组件: {token}"


@pytest.mark.unit
def test_demo_dependencies_present_and_marked_demo():
    demo = K8S / "demo" / "dependencies.yaml"
    assert demo.exists()
    text = demo.read_text(encoding="utf-8")
    assert "DEMO ONLY" in text
    kinds = {d.get("kind") for d in _docs(demo)}
    assert {"Deployment", "Service"} <= kinds
