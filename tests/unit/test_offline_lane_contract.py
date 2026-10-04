"""默认测试 lane 的离线契约（issue #52）。

两层保证：

1. **配置层**：``.env.test``（``make test`` 实际加载的那份）必须声明离线 lane ——
   RAG provider 走确定性 embedder，且不携带任何「看起来可用」的 provider 凭据。
   占位 key 的危害不在于值本身，而在于它让代码**以为** provider 可达，于是每次
   检索都真的发出一次请求。
2. **执行层**：``tests/offline_guard.py`` 在 socket 层拦截非回环 connect，并且
   按记录判定整轮结果 —— 即使异常被应用代码吞掉也不会假绿。

这些断言在 #52 之前全部失败（``.env.test`` 里是 ``sk-placeholder-*``，
conftest 里没有任何出网守卫）。
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from tests import offline_guard

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_TEST = REPO_ROOT / ".env.test"


def _env_test_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in ENV_TEST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip()
    return values


class TestEnvTestDeclaresOfflineLane:
    def test_embedding_provider_is_offline(self):
        assert _env_test_values()["EMBEDDING_PROVIDER"] == "offline"

    def test_reranker_key_is_blank_so_the_provider_is_honestly_unavailable(self):
        """空 key → ApiReranker ``available=False`` 且**不发请求**。

        原来这里是占位 key：reranker「看起来可用」→ 每次检索打一次
        api.siliconflow.cn → 401 → 检索被标 degraded。测试结果因此依赖第三方可达性。
        """
        assert _env_test_values()["RERANKER_API_KEY"] == ""

    @pytest.mark.parametrize("key", ["OPENAI_API_KEY", "EMBEDDING_API_KEY", "RERANKER_API_KEY"])
    def test_no_provider_credential_looks_usable(self, key):
        """默认 lane 只能携带**空**或**显式占位**凭据 —— 真实 key 绝不能进仓库。

        占位 key 的危害不在于值本身，而在于它让代码**以为** provider 可达。
        ``RERANKER_API_KEY`` 进一步收紧为空：ApiReranker 在无 key 时诚实地报告
        ``available=False`` 且不发请求。
        """
        value = _env_test_values()[key]
        assert value == "" or value.startswith("sk-placeholder"), (
            f"{key}={value!r} 既不是空值也不是显式占位符：默认 lane 不得携带可用的 provider 凭据"
        )


class TestOfflineGuardMechanism:
    """守卫本身的行为 —— 它必须真的拦得住，否则整条契约形同虚设。"""

    @staticmethod
    def _forget_violations() -> None:
        offline_guard.VIOLATIONS.clear()

    def teardown_method(self):
        offline_guard.uninstall()
        self._forget_violations()

    def test_non_loopback_connect_is_blocked_and_recorded(self):
        """这条在守卫缺失时不可能通过：连接会真的发出去（或直接超时）。

        用数字字面量 IP + 裸 socket，**不经过 DNS**：这样断网环境（解析器直接
        失败）下也能确定性地走到 socket 层，测的确实是守卫而不是 resolver。
        """
        before = len(offline_guard.VIOLATIONS)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(offline_guard.OfflineLaneViolation):
                sock.connect(("198.18.0.7", 443))
        finally:
            sock.close()
        recorded = offline_guard.VIOLATIONS[before:]
        assert len(recorded) == 1
        assert recorded[0]["host"] == "198.18.0.7"
        assert recorded[0]["port"] == 443

    def test_connect_ex_is_blocked_too(self):
        before = len(offline_guard.VIOLATIONS)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(offline_guard.OfflineLaneViolation):
                sock.connect_ex(("198.18.0.8", 443))
        finally:
            sock.close()
        assert len(offline_guard.VIOLATIONS[before:]) == 1

    def test_ipv6_non_loopback_is_blocked(self):
        before = len(offline_guard.VIOLATIONS)
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            with pytest.raises(offline_guard.OfflineLaneViolation):
                sock.connect(("2620:1ec:33::10", 443))
        finally:
            sock.close()
        assert len(offline_guard.VIOLATIONS[before:]) == 1

    def test_loopback_is_allowed(self):
        """守卫不能误伤本地基础设施（Qdrant / Redis / 临时 SQLite 服务）。

        回环上一个没人监听的端口 → ConnectionRefusedError（说明请求确实到了
        socket 层，而不是被守卫拦下）。
        """
        offline_guard.install(owner="loopback-test")
        before = len(offline_guard.VIOLATIONS)
        with pytest.raises((ConnectionRefusedError, OSError)) as exc:
            socket.create_connection(("127.0.0.1", 1), timeout=0.5)
        assert not isinstance(exc.value, offline_guard.OfflineLaneViolation)
        assert len(offline_guard.VIOLATIONS[before:]) == 0

    def test_uninstall_restores_the_original_socket_connect(self):
        # 先摘掉 autouse 守卫（depth 1 → 0），拿到真正的原始实现再验证往返。
        offline_guard.uninstall()
        original = socket.socket.connect
        offline_guard.install(owner="install-test")
        assert socket.socket.connect is not original
        offline_guard.uninstall()
        assert socket.socket.connect is original

    def test_violation_is_an_assertion_error(self):
        """必须是 AssertionError 子类，pytest 才会当成失败而不是错误跳过。"""
        assert issubclass(offline_guard.OfflineLaneViolation, AssertionError)


class TestSessionGateIsTestable:
    """「异常被吞掉」那条假绿通道的判定逻辑（conftest 的 hook 直接调用它）。"""

    def test_no_violation_produces_no_report(self):
        assert offline_guard.report([]) is None

    def test_violation_produces_report_naming_the_host_and_the_test(self):
        text = offline_guard.report(
            [{"host": "api.siliconflow.cn", "port": 443, "how": "connect", "owner": "t.py::t"}]
        )
        assert text is not None
        assert "api.siliconflow.cn:443" in text
        assert "t.py::t" in text
        assert (
            offline_guard.report(
                [
                    {"host": "h", "port": 1, "how": "connect", "owner": "a"},
                    {"host": "h", "port": 1, "how": "connect", "owner": "b"},
                ]
            ).count("h:1")
            == 1
        ), "同一个 host:port 只报一次"


class TestGuardWiring:
    def test_conftest_installs_the_guard_automatically(self):
        conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
        assert "offline_guard.install" in conftest
        assert "autouse=True" in conftest

    def test_real_llm_marker_is_the_documented_opt_out(self):
        assert offline_guard.NETWORK_MARKER == "real_llm"

    def test_escape_hatch_is_opt_in_not_default(self, monkeypatch):
        monkeypatch.delenv(offline_guard.ENV_ALLOW_NETWORK, raising=False)
        assert offline_guard.allow_network_env() is False
        monkeypatch.setenv(offline_guard.ENV_ALLOW_NETWORK, "1")
        assert offline_guard.allow_network_env() is True

    def test_escape_hatch_is_documented_in_the_module(self):
        source = (REPO_ROOT / "tests" / "offline_guard.py").read_text(encoding="utf-8")
        assert offline_guard.ENV_ALLOW_NETWORK in source


class TestNoRealProviderHostsInTestLaneConfig:
    @pytest.mark.parametrize("host", ["api.siliconflow.cn", "api.openai.com"])
    def test_default_lane_never_needs_a_reachable_provider(self, host):
        """默认 lane 的 embedding provider 不指向任何真实 provider 域名。

        ``EMBEDDING_BASE_URL`` 仍然保留 siliconflow 是有意的：它是 ``api`` 模式的
        默认目标，写在这里是为了让「切回真实 provider」只需改一个变量；但
        ``EMBEDDING_PROVIDER=offline`` 意味着这个地址在默认 lane 下永远不会被访问。
        """
        values = _env_test_values()
        assert values["EMBEDDING_PROVIDER"] == "offline"
        # LLM 在 DEV_MODE 下由规则引擎兜底，不发 HTTP；确认没有真实 key。
        assert values["OPENAI_API_KEY"].startswith("sk-placeholder")
        assert os.path.isfile(ENV_TEST)
