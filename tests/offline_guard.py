"""默认测试 lane 的出网守卫（issue #52）。

`make test` 对外承诺「离线、无需 API Key」。这条承诺以前只是一句文档：默认 lane
会真的去连 ``api.siliconflow.cn``（embedding + rerank + ASR）和
``speech.platform.bing.com``（edge-tts）。守卫把承诺变成可执行的不变量。

为什么必须挂在 socket 层
------------------------
只在「会出网的客户端」上加断言不够：``ApiReranker.rerank_with_outcome`` 有一句
``except Exception`` 会把任何传输层异常降级成一次「provider 失败」。也就是说，
一个只抛异常的守卫可能被应用代码吞掉，测试照样绿 —— 正是本 issue 之前的状态。
所以这里双管齐下：

1. 非回环 ``connect()`` 立刻抛 :class:`OfflineLaneViolation`（绝大多数情况直接
   让用例失败）；
2. 每次违规都记进 :data:`VIOLATIONS`，``pytest_sessionfinish`` 再按记录判定，
   **即使异常被吞掉也会让整轮 run 失败**。

豁免
----
带 ``real_llm`` 标记的用例（真实 provider lane）不做限制；调试时可用
``CSAI_TEST_ALLOW_NETWORK=1`` 整体关闭守卫 —— 两者都会在 session 报告里显式
标注，不会静默生效。

已知边界：守卫只覆盖本进程。``subprocess`` 拉起的子进程（如
``tests/unit/test_ci_contract.py`` 里的 bash）不在守卫范围内。
"""

from __future__ import annotations

import os
import socket
from typing import Any

__all__ = [
    "OfflineLaneViolation",
    "install",
    "uninstall",
    "violations",
    "VIOLATIONS",
    "allow_network_env",
]

#: 允许标记真实 provider 的 marker（与 CI `-m "not real_llm"` 分层一致）
NETWORK_MARKER = "real_llm"

#: 调试用总开关。置 1 时守卫整体停用，session 报告会记录这一事实。
ENV_ALLOW_NETWORK = "CSAI_TEST_ALLOW_NETWORK"

_LOOPBACK_HOSTS = frozenset({"", "localhost", "0.0.0.0", "::", "::1"})


class OfflineLaneViolation(AssertionError):
    """默认测试 lane 试图连接非回环地址。"""


#: 进程级违规记录（append-only）。``pytest_sessionfinish`` 会据此判定整轮结果。
VIOLATIONS: list[dict[str, Any]] = []


class _Guard:
    """可重复安装/卸载的 socket.connect 守卫。"""

    def __init__(self) -> None:
        self._orig_connect: Any = None
        self._orig_connect_ex: Any = None
        self.depth = 0

    def install(self, *, owner: str = "") -> None:
        self.depth += 1
        if self.depth > 1:
            return
        self._orig_connect = socket.socket.connect
        self._orig_connect_ex = socket.socket.connect_ex
        guard = self

        def _is_local(address: Any) -> bool:
            if not isinstance(address, tuple) or len(address) < 2:
                return True
            host = address[0]
            if not isinstance(host, str):
                return True
            return host.lower() in _LOOPBACK_HOSTS or host.startswith("127.")

        def _connect(sock: socket.socket, address: Any, *args: Any, **kwargs: Any):
            if _is_local(address):
                return guard._orig_connect(sock, address, *args, **kwargs)
            guard._record(address, owner, "connect")
            raise OfflineLaneViolation(
                f"默认测试 lane 试图出网连接 {address}（{owner}）。"
                f"`make test` 必须是离线 lane：请改用本地 double / "
                f"{NETWORK_MARKER} 标记，或设置 {ENV_ALLOW_NETWORK}=1 显式放行。"
            )

        def _connect_ex(sock: socket.socket, address: Any, *args: Any, **kwargs: Any):
            if _is_local(address):
                return guard._orig_connect_ex(sock, address, *args, **kwargs)
            guard._record(address, owner, "connect_ex")
            raise OfflineLaneViolation(
                f"默认测试 lane 试图出网连接 {address}（{owner}）。`make test` 必须是离线 lane。"
            )

        socket.socket.connect = _connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]

    def uninstall(self) -> None:
        if self.depth == 0:
            return
        self.depth -= 1
        if self.depth > 0:
            return
        if self._orig_connect is not None:
            socket.socket.connect = self._orig_connect  # type: ignore[method-assign]
        if self._orig_connect_ex is not None:
            socket.socket.connect_ex = self._orig_connect_ex  # type: ignore[method-assign]
        self._orig_connect = None
        self._orig_connect_ex = None

    @staticmethod
    def _record(address: Any, owner: str, how: str) -> None:
        try:
            host, port = address[0], address[1]
        except (TypeError, IndexError):
            host, port = repr(address), -1
        VIOLATIONS.append({"host": str(host), "port": int(port), "how": how, "owner": owner})


_GUARD = _Guard()


def install(*, owner: str = "") -> _Guard:
    return _GUARD.install(owner=owner) or _GUARD


def uninstall() -> None:
    _GUARD.uninstall()


def violations() -> list[dict[str, Any]]:
    return list(VIOLATIONS)


def allow_network_env() -> bool:
    return os.getenv(ENV_ALLOW_NETWORK) == "1"


def report(records: list[dict[str, Any]] | None = None) -> str | None:
    """把违规记录渲染成报告文本；没有违规时返回 ``None``。

    决策逻辑放在这里（而不是 conftest 的 hook 里）是为了能被单测直接覆盖：
    「异常被应用代码吞掉」这条假绿通道正是靠这个函数兜住的。
    """
    rows = VIOLATIONS if records is None else records
    if not rows:
        return None
    lines = ["", "=" * 72, "OFFLINE LANE VIOLATION — 默认测试 lane 发生了真实出网"]
    seen: set[tuple[str, int]] = set()
    for v in rows:
        key = (str(v["host"]), int(v["port"]))
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  {key[0]}:{key[1]}  ← {v.get('owner', '')}")
    lines.append("  修复方式：用本地 double 替换该传输，或把用例移入 real_llm 标记的 lane。")
    lines.append("=" * 72)
    return "\n".join(lines)
