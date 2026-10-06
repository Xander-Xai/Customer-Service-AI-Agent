"""MCP 端到端契约证据：registry → adapter → 本地 fake server → result → policy/telemetry。

这条测试驱动的**每一段都是真的**，没有一段是 mock 出来的：

===========================  ====================================================
环节                          实现
===========================  ====================================================
ToolRegistry                 本仓 ``tools/tool_registry.py`` 真实实例
MCP adapter                  本仓 ``tools/mcp_adapter.py`` 真实实现
client 侧协议实现              **官方** ``mcp`` SDK（``mcp.client.stdio`` + ``ClientSession``）
server 侧协议对端              本仓 ``tests/integration/fake_mcp_server.py``
                             （MCP stdio wire protocol，换行分隔 JSON-RPC）
传输                          真实子进程 + 真实 stdin/stdout 管道
===========================  ====================================================

**不依赖任何外部公开 MCP 服务。** 唯一的外部依赖是本仓自己写的 fake server 文件，
因此这个契约在离线 CI 里可复现，也不会因为第三方服务限流/改协议而变红。

覆盖的 5 类工具行为（对应 MCP 接入必须回答的 5 个问题）：

1. **safe read** —— 只读工具能否端到端取回结果，且结果确定；
2. **high-risk side effect** —— 写操作工具在注册阶段是否被 fail-closed 拦住；
3. **timeout** —— 超时是否稳定收敛到 ``MCPTimeoutError``，且**整个 timeout
   lifecycle（含 close 也就是 teardown）有硬上限**：close 回归成"关不掉"时，
   套件仍会结束，且不残留 fake MCP server 子进程；
4. **oversized** —— 请求侧 payload 上限是否真的生效（并如实刻画响应侧缺口）；
5. **failing** —— server 报告的失败是否稳定收敛到 ``MCPToolExecutionError``。

另外断言 policy（风险等级 / 审批闸门）与 telemetry（Prometheus 标签值）两条腿。

**teardown 硬上限**（见下方「teardown 硬上限」一节的完整推导）是这份证据的
一部分，不是装饰：``adapter.close()`` 自身没有超时，fixture teardown 若裸
``await close()``，一次 close 回归就会让整套 CI 永远跑不完。

证据边界见 ``docs/reference/current-state.md`` 的 MCP 段落与
``docs/interview/failure-and-tradeoffs.md`` §7。**真实第三方 MCP server、生产连通性
与写操作 MCP 工具都是 ``NOT_VERIFIED`` / ``NOT_IMPLEMENTED``**，本文件不声称覆盖。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from core.hitl.risk import RiskLevel
from tools.mcp_adapter import (
    MCPPayloadTooLargeError,
    MCPServerConfig,
    MCPTimeoutError,
    MCPToolAdapter,
    MCPToolExecutionError,
    MCPUnauthorizedToolError,
    build_mcp_adapters,
    register_mcp_tools,
)
from tools.tool_registry import SOURCE_MCP, SOURCE_NATIVE, ToolRegistry

pytestmark = pytest.mark.integration

#: 官方 SDK 是 client 侧协议的**唯一**实现，缺了它这条契约无从谈起。
#: 用 ``importorskip`` 而不是静默通过：跳过的证据不是证据。
pytest.importorskip("mcp", reason="官方 mcp SDK 未安装：MCP 端到端契约无法验证")

FAKE_SERVER = Path(__file__).with_name("fake_mcp_server.py")

#: fake server 暴露的全部工具名（含故意不在 allowlist 里的那个）。
ALL_FAKE_TOOLS = (
    "get_product_info",
    "issue_refund",
    "slow_lookup",
    "bulk_dump",
    "flaky_lookup",
    "secret_admin_tool",
)

#: ``get_product_info`` 的**精确**确定性输出。写死在这里有两个作用：既验证结果
#: 正确，也验证 fake server 确实确定性（同一个 sku 必须每次得到同一串字节）。
EXPECTED_READ_RESULT = '{"in_stock": true, "name": "测试商品 SKU-1", "price": 99.0, "sku": "SKU-1"}'


def _counter(name: str, **labels: str) -> float:
    """读一个 Prometheus counter 的当前值（未自增时为 0.0）。"""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels) or 0.0


def _config(
    *,
    name: str = "fake",
    risk_level: Any = "low",
    timeout_seconds: float = 5.0,
    max_payload_bytes: int = 32768,
    allowed_tools: tuple[str, ...] = ALL_FAKE_TOOLS,
) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=(str(FAKE_SERVER),),
        allowed_tools=allowed_tools,
        timeout_seconds=timeout_seconds,
        max_payload_bytes=max_payload_bytes,
        risk_level=risk_level,
    )


# ==========================================================================
# teardown 硬上限
# ==========================================================================
#
# 这一节只服务于**测试基础设施**：不碰适配器行为，也不引入新依赖。
#
# ## 要防的回归
#
# ``slow_lookup`` 会让 fake server 固定挂 30s（``fake_mcp_server.SLOW_SLEEP_SECONDS``），
# 而 ``adapter.close()`` 自身**没有任何超时**。一旦 close 回归成「关不掉」，
# fixture teardown 里那句裸 ``await adapter.close()`` 就会让整个套件永久挂死。
#
# ## 进程内为什么不能用任何 asyncio 超时原语（本仓实测的坑）
#
# MCP SDK 的 anyio cancel scope 绑定在**进入 ``__aenter__`` 的那个 task** 上。
# 于是：
#
# - ``asyncio.wait_for(close(), t)`` / ``ensure_future(close())`` 会把 close 搬进一个
#   **新 task**，``__aexit__`` 立刻抛
#   ``RuntimeError: Attempted to exit cancel scope in a different task``。
#   close 走的是 best-effort 的 ``except Exception``，于是它**静默变成 no-op**：
#   session 没关掉，而测试看上去是绿的。（实测：这样跑会留下活着的 server，
#   并且在日志里留下 ``MCP SDK close 异常: RuntimeError``。）
# - 就算不换 task，Python 3.10 的 ``wait_for`` 超时后走 ``_cancel_and_wait``：先 cancel
#   **再 await 该 task 真正结束**。而 ``McpSdkClient.close`` 与 ``MCPToolAdapter.close``
#   都显式吞掉 ``CancelledError``（cleanup 是 best-effort 的，绝不能把「关一个 MCP
#   session」升级成「整个进程 shutdown 失败」）。两者叠加 → close 一卡死，``wait_for``
#   跟着永久挂死。
# - ``asyncio.wait({task}, timeout=...)`` 会按时返回，但把一个 **pending task** 留在
#   loop 上。pytest-asyncio 1.4 用 ``asyncio.Runner`` 托管 loop，其 ``__exit__`` 会
#   ``_cancel_all_tasks`` → ``gather(*to_cancel)``；一个无视取消的 pending task 让这个
#   gather **永远不返回** → 套件照样挂死。（实测：留一个这样的 task 之后，套件里后续
#   的测试再也不会运行。）
#
# 结论：**进程内不存在能真正给 close 卡上时间的 asyncio 原语。**
#
# ## 于是分两层，各管各的
#
# 1. :class:`_StuckServerWatchdog` —— 进程内、**不碰 event loop**。它是一个普通
#    ``threading.Timer``：到点就从外面杀掉本进程拉起的 fake MCP server，让**同 task**
#    的 close 自己解开。它不取消协程，所以不破坏 SDK 的 cancel-scope 契约。作用是把
#    「子进程不肯死」这一真实卡死成因从「挂死」降级成「被杀干净 + 明确报错」。
# 2. :func:`_run_timeout_lifecycle` —— 进程外，**唯一无条件的上限**。整个 timeout
#    lifecycle（connect → 超时 invoke → close，**close 就是 teardown**）跑在一个
#    ``start_new_session=True`` 的子进程里；父进程 ``wait(timeout=...)`` 卡死线，超时
#    就 ``killpg(SIGKILL)`` 整个进程组。连「close 无视取消」这种进程内无解的形状也会被
#    物理消灭，且同组的 fake MCP server 一并带走。
#
# 进程内扫描用 Linux ``/proc``（``psutil`` 不是本仓依赖，不为测试新增依赖）。非 Linux
# 平台上这些函数退化为 no-op，此时第 2 层的硬上限仍然成立。

#: 单个 ``close()`` 的看门狗预算。取值必须**高于** SDK 自身最坏情况的 teardown
#: 花费，否则健康路径会被误判成 hang：``mcp.client.stdio`` 的
#: ``_stop_server_process`` 是 ``PROCESS_TERMINATION_TIMEOUT(2s)`` +
#: ``FORCE_KILL_TIMEOUT(2s)`` + ``_KILL_REAP_TIMEOUT(2s)`` + 写端 flush(0.5s)，
#: 合计约 6.5s 封顶。留出余量后取 15s。
CLOSE_WATCHDOG_SECONDS = 15.0

#: 进程外整个 timeout lifecycle 的硬上限。
LIFECYCLE_HARD_BOUND_SECONDS = 60.0

#: 杀掉进程之后，等待 ``/proc`` 里对应条目消失的轮询上限。
REAP_SETTLE_SECONDS = 5.0

#: 子进程 → 父进程的���向事件通道前缀（stdout，一行一个事件）。
DRIVER_PREFIX = "MCP_LIFECYCLE="


# ===== fake MCP server 子进程的发现与回收 =====


def _read_stat(pid: int) -> tuple[int, int] | None:
    """读 ``/proc/<pid>/stat`` 的 ``(ppid, session_id)``；进程已消失返回 None。

    ``comm``（第 2 字段）带括号且可能含空格/括号，所以必须按**最后一个** ``)`` 切开，
    再按空白切分。切开后的下标：``[0]=state [1]=ppid [2]=pgrp [3]=session``。
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
        rest = raw[raw.rindex(")") + 1 :].split()
        return int(rest[1]), int(rest[3])
    except (OSError, ValueError, IndexError):
        return None


def _mentions_fake_server(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv = handle.read().split(b"\0")
    except OSError:
        return False
    return any(part.endswith(FAKE_SERVER.name.encode()) for part in argv)


def _ancestors(pid: int, *, limit: int = 64) -> set[int]:
    """pid 的祖先链（含自己），用来把 server 归属到拉起它的那个进程。

    限定祖先而不是"全机器按 cmdline 扫"：并发的另一份本套件也会起同样的 server，
    只按 cmdline 匹配会互相误杀。
    """
    chain: set[int] = set()
    current = pid
    for _ in range(limit):
        stat = _read_stat(current)
        if stat is None or current in chain or current <= 1:
            break
        chain.add(current)
        current = stat[0]
    return chain


def _proc_pids() -> list[int]:
    if not sys.platform.startswith("linux"):
        return []
    try:
        return [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:  # pragma: no cover - /proc 不可读时退化为"无泄漏可清"
        return []


def _own_fake_servers() -> set[int]:
    """本进程拉起、且此刻仍存活的 fake MCP server pid。"""
    me = os.getpid()
    mine = _ancestors(me)
    return {
        pid
        for pid in _proc_pids()
        if pid != me and _mentions_fake_server(pid) and (_ancestors(pid) & mine)
    }


def _fake_servers_in_session(sid: int) -> set[int]:
    """仍活在这个 session id 里的 fake MCP server pid（进程外泄漏的判定口径）。

    进程外 harness 用 ``start_new_session=True`` 让子进程当 session leader，它拉起的
    fake server 继承同一个 sid。因此"整组 SIGKILL 之后这个 sid 下还有没有活人"就是一个
    精确判据 —— 它不依赖父子血缘（父进程已被杀，血缘会断链）。
    """
    found = set()
    for pid in _proc_pids():
        if not _mentions_fake_server(pid):
            continue
        stat = _read_stat(pid)
        if stat is not None and stat[1] == sid:
            found.add(pid)
    return found


def _pid_alive(pid: int) -> bool:
    return _read_stat(pid) is not None


def _kill_pids(pids: set[int]) -> None:
    """SIGTERM → 宽限 → SIGKILL。回收孤儿 server 不值得为它优雅。"""
    for pid in pids:
        _kill_one(pid, signal.SIGTERM)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and any(_pid_alive(pid) for pid in pids):
        time.sleep(0.02)
    for pid in pids:
        if _pid_alive(pid):
            _kill_one(pid, signal.SIGKILL)


def _kill_one(pid: int, sig: int) -> None:
    """进程可能已经自己没了 —— 那种 ``ProcessLookupError`` 是正常结果，不是错误。"""
    with contextlib.suppress(OSError):
        os.kill(pid, sig)


def _await_gone(pids: set[int], *, budget: float) -> set[int]:
    """轮询等这些 pid 消失，返回**仍然存活**的那些。"""
    survivors = set(pids)
    deadline = time.monotonic() + budget
    while survivors and time.monotonic() < deadline:
        survivors = {pid for pid in survivors if _pid_alive(pid)}
        if survivors:
            time.sleep(0.02)
    return {pid for pid in survivors if _pid_alive(pid)}


# ===== 进程内：close 看门狗（不建 asyncio task）=====


class _StuckServerWatchdog:
    """到点就从**事件循环之外**杀掉本进程拉起的 fake MCP server。

    为什么必须是线程而不是 ``asyncio.wait_for``：MCP SDK 的 anyio cancel scope 绑定在
    那个进入 ``__aenter__`` 的 task 上，把 close 换到任何**别的** task 里执行都会让
    ``__aexit__`` 抛 cancel-scope ``RuntimeError``（被 best-effort 的
    ``except Exception`` 吞掉 → 静默不关）。而 ``wait_for`` 即便不换 task，也会在超时
    后 ``await`` 被取消的 task，配合 close 吞 ``CancelledError`` 一起挂死。详见本节
    开头。

    所以进程内唯一不破坏 SDK 契约的做法是：不动协程、不取消它，只把"它等的那个子进程"
    杀掉，让它自己解开。这也精确对应真实卡死成因 —— close 关不掉，几乎总是因为那个
    stdio 子进程不肯死。

    它**不是**硬上限：若 close 卡在与子进程无关的地方，它救不回来。无条件保证由
    :func:`_run_timeout_lifecycle` 在进程外提供。
    """

    def __init__(self, *, budget: float, label: str = "close") -> None:
        self._budget = budget
        self._label = label
        self._timer: threading.Timer | None = None
        self._started = threading.Event()
        self.killed: frozenset[int] = frozenset()

    def __enter__(self) -> _StuckServerWatchdog:
        def _fire() -> None:
            self._started.set()
            stuck = _own_fake_servers()
            if not stuck:
                return
            # **先记账再动手**：close 只有在子进程真的死掉之后才可能返回，所以它一定
            # 读得到这里已经写好的 ``killed``。反过来（先 kill 后记账）会留下一个
            # 窗口：close 已经被救回来、但 ``killed`` 还没写完，调用方就会把一次
            # "靠看门狗救回来的 close" 误判成"干净关闭"。
            self.killed = frozenset(stuck)
            _kill_pids(stuck)

        self._timer = threading.Timer(self._budget, _fire)
        # daemon：看门狗本身绝不能成为让进程不退出的原因。
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._timer is None:
            return
        self._timer.cancel()
        # 只有线程真的跑起来了才等它：没跑起来时 cancel 已经足够，等下去是白等。
        if self._started.is_set():
            self._timer.join(timeout=REAP_SETTLE_SECONDS)
        self._timer = None

    @property
    def fired(self) -> bool:
        return bool(self.killed)

    def __repr__(self) -> str:  # pragma: no cover - 只在失败信息里用
        return f"_StuckServerWatchdog(label={self._label!r}, budget={self._budget}s)"


@dataclasses.dataclass(frozen=True)
class CloseOutcome:
    """一次被看门狗看守的 close 的结果。

    ``clean`` 表示 close **自己**在预算内结束了；``False`` 表示它是靠看门狗杀掉子进程
    才回来的 —— 那本身就是需要修的回归。
    """

    clean: bool
    elapsed: float
    killed: frozenset[int] = frozenset()


async def _close_watched(
    adapter: MCPToolAdapter, *, budget: float, label: str = "close"
) -> CloseOutcome:
    """在**同一个 task** 里关闭 adapter，同时用看门狗兜住"子进程不肯死"。

    刻意不把 close 放进任何 asyncio 超时原语：见本节开头，进程内没有能真正给 close
    卡时间的原语，硬套只会把"挂死"换成"静默不关"或"照样挂死"。
    """
    started = time.monotonic()
    with _StuckServerWatchdog(budget=budget, label=label) as watchdog:
        await adapter.close()
    return CloseOutcome(
        clean=not watchdog.fired,
        elapsed=time.monotonic() - started,
        killed=watchdog.killed,
    )


async def _shutdown_adapters(adapters: list[MCPToolAdapter]) -> None:
    """fixture teardown 的实现：被看门狗看守的关闭 + 关不掉就大声失败。

    "失败"优于"挂住"：挂住会让整套 CI 永远跑不完，而且看不出是哪条用例。
    """
    rescued: list[tuple[int, CloseOutcome]] = []
    for index, adapter in enumerate(adapters):
        outcome = await _close_watched(
            adapter, budget=CLOSE_WATCHDOG_SECONDS, label=f"teardown[{index}]"
        )
        if not outcome.clean:
            rescued.append((index, outcome))

    if _own_fake_servers():
        leftovers = _own_fake_servers()
        _kill_pids(leftovers)
        still = _await_gone(leftovers, budget=REAP_SETTLE_SECONDS)
        if still:  # pragma: no cover - SIGKILL 都杀不掉才会出现
            raise RuntimeError(f"teardown: fake MCP server 在 SIGKILL 后仍存活 {sorted(still)}")

    if rescued:
        detail = ", ".join(
            f"#{index} 用时 {outcome.elapsed:.1f}s（杀掉 {sorted(outcome.killed)}）"
            for index, outcome in rescued
        )
        raise RuntimeError(
            f"teardown: {len(rescued)}/{len(adapters)} 个 adapter 没在 "
            f"{CLOSE_WATCHDOG_SECONDS}s 内自己关掉（{detail}），是靠杀掉泄漏的 fake MCP "
            "server 才回来的。close 卡住本身就是需要修的回归。"
        )


# ===== 进程外：整个 timeout lifecycle 的硬上限 =====


@dataclasses.dataclass(frozen=True)
class LifecycleResult:
    """进程外 lifecycle 的一次运行结果。

    ``status``:

    - ``completed`` —— 子进程跑完并正常退出（close 也干净收尾了）；
    - ``hard_killed`` —— 触碰硬上限，父进程 ``killpg(SIGKILL)`` 整组带走；
    - ``crashed`` —— 子进程非零退出，且没到上限。
    """

    status: str
    returncode: int | None
    elapsed: float
    events: tuple[str, ...]
    survivors: frozenset[int]
    stdout: str
    stderr: str

    def saw(self, event: str) -> bool:
        return event in self.events

    def reported_servers(self) -> list[int]:
        """子进程自报的"我拉起了哪些 fake MCP server"。

        信任子进程自报而不是父进程事后扫描：只有自报才能在"父进程把子进程组杀掉、
        血缘断链"之前留下一份对比对象，否则"没有残留"会退化成"本来就没有"的空断言。
        """
        pids: list[int] = []
        for event in self.events:
            if not event.startswith("servers="):
                continue
            pids.extend(
                int(part) for part in event[len("servers=") :].strip("[]").split(",") if part
            )
        return pids


def _run_timeout_lifecycle(
    *, mode: str, bound: float = LIFECYCLE_HARD_BOUND_SECONDS
) -> LifecycleResult:
    """把整个 timeout lifecycle 放进子进程跑，父进程持硬上限。

    ``mode``:

    - ``"clean"`` —— 正常路径：connect → 超时 invoke → close 并干净收尾；
    - ``"hang"`` —— 回归路径：close 永不返回（且无视取消），用来证明套件仍然结束得了、
      且 fake MCP server 不会留下来。

    父进程只做三件事：``setsid`` 起子进程、``wait(timeout=bound)``、超了就
    ``killpg(SIGKILL)``。因为 fake MCP server 继承子进程的 session，进程组被物理消灭时
    它一定一起消失 —— 这就是"超时后不留子进程"这条要求的无条件保证。
    """
    proc = subprocess.Popen(  # noqa: S603 - argv 固定，无 shell
        [sys.executable, "-m", "tests.integration.test_mcp_contract_e2e", "--lifecycle", mode],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    assert proc.pid is not None
    started = time.monotonic()
    try:
        stdout, stderr = proc.communicate(timeout=bound)
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        # 子进程可能刚好自己退了 —— 那种 ESRCH 是正常结果，不是错误。
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = _reap(proc)
        return LifecycleResult(
            status="hard_killed",
            returncode=proc.returncode,
            elapsed=elapsed,
            events=_driver_events(stdout),
            survivors=frozenset(_await_gone(_fake_servers_in_session(proc.pid), budget=2.0)),
            stdout=stdout,
            stderr=stderr,
        )

    return LifecycleResult(
        status="completed" if proc.returncode == 0 else "crashed",
        returncode=proc.returncode,
        elapsed=time.monotonic() - started,
        events=_driver_events(stdout),
        # 正常路径也查一次：干净收尾不该留下任何同 session 的 server。
        survivors=frozenset(_fake_servers_in_session(proc.pid)),
        stdout=stdout,
        stderr=stderr,
    )


def _reap(proc: subprocess.Popen) -> tuple[str, str]:
    """SIGKILL 之后有界地把子进程的管道读到 EOF（``communicate`` 不会自己收场）。"""
    try:
        return proc.communicate(timeout=REAP_SETTLE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - killpg 之后不应发生
        proc.kill()
        return proc.communicate()


def _driver_events(stdout: str) -> tuple[str, ...]:
    return tuple(
        line[len(DRIVER_PREFIX) :].strip()
        for line in stdout.splitlines()
        if line.startswith(DRIVER_PREFIX)
    )


def _driver_emit(event: str) -> None:
    """子进程 → 父进程的单向事件通道（每行 flush，父进程随时可读）。"""
    print(f"{DRIVER_PREFIX}{event}", flush=True)


# ===== 子进程侧：被 harness 驱动的 lifecycle =====


async def _never_closing() -> None:
    """``hang`` 模式替换 close 用的协程：永不返回。

    用 ``asyncio.Event().wait()`` 而不是 ``time.sleep``：前者是**可取消**的 await 点，
    所以它精确复现"close 吞掉取消之后继续卡着"这个形状，而不会顺带把子进程的
    event loop 变成不可中断（那样连"卡住"都不是真的了）。
    """
    await asyncio.Event().wait()


async def _driver_run(mode: str) -> int:
    adapter = MCPToolAdapter(_config(timeout_seconds=0.5, allowed_tools=("slow_lookup",)))
    await adapter.connect()
    # 先把"确实有一个 fake MCP server 活着"交给父进程当对比对象。
    _driver_emit(f"servers={sorted(_own_fake_servers())}")

    try:
        await adapter.invoke("slow_lookup", {"q": "x"})
    except MCPTimeoutError:
        _driver_emit("timeout_observed")
    else:  # pragma: no cover - 契约一旦回归，父进程会看到缺少该事件并报错
        _driver_emit("timeout_missing")

    if mode == "hang":
        adapter.close = _never_closing  # type: ignore[method-assign]
    await adapter.close()
    _driver_emit("closed_cleanly")
    return 0


def _driver_main(mode: str) -> int:
    if mode not in {"clean", "hang"}:  # pragma: no cover - 只由 harness 调用
        raise SystemExit(f"unknown lifecycle mode: {mode!r}")
    return asyncio.run(_driver_run(mode))


@pytest.fixture
async def adapter_factory():
    """建 adapter 并保证 teardown（否则 stdio 子进程会泄漏成孤儿进程）。

    teardown 走 :func:`_shutdown_adapters` 而不是裸 ``await adapter.close()``：
    **fixture teardown 必须自带硬上限**。原因见上方「teardown 硬上限」一节——
    没有上限时，一次 close 回归就会让整个套件永远结束不了。
    """
    created: list[MCPToolAdapter] = []

    def _make(**kwargs: Any) -> MCPToolAdapter:
        adapter = MCPToolAdapter(_config(**kwargs))
        created.append(adapter)
        return adapter

    yield _make

    await _shutdown_adapters(created)


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


# ===== 1. safe read：完整链路 + 确定性 =====


class TestSafeReadContract:
    async def test_read_tool_round_trips_through_the_whole_chain(self, adapter_factory, registry):
        """ToolRegistry.execute → adapter → 官方 SDK client → fake server → 结果。"""
        adapter = adapter_factory()
        registered = await register_mcp_tools(registry, adapter)
        assert "mcp__fake__get_product_info" in registered

        result = await registry.execute("mcp__fake__get_product_info", {"sku": "SKU-1"})
        assert result == EXPECTED_READ_RESULT

    async def test_read_result_is_deterministic_across_calls(self, adapter_factory, registry):
        """同样的输入必须得到同样的字节 —— 否则它不是可断言的契约。"""
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)
        first = await registry.execute("mcp__fake__get_product_info", {"sku": "SKU-1"})
        second = await registry.execute("mcp__fake__get_product_info", {"sku": "SKU-1"})
        assert first == second == EXPECTED_READ_RESULT

    async def test_mcp_tools_are_namespaced_and_exposed_to_function_calling(
        self, adapter_factory, registry
    ):
        """Agent 侧只看统一 ToolDefinition：命名空间化 + 进入 get_openai_tools。"""
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)

        names = registry.list_tools()
        assert "mcp__fake__get_product_info" in names
        # 命名空间化后不得与任何 native 工具重名
        assert all(n.startswith("mcp__fake__") for n in names)

        fc_names = {t["function"]["name"] for t in registry.get_openai_tools()}
        assert "mcp__fake__get_product_info" in fc_names
        # schema 被归一化成 JSON Schema object，LLM 才能据此生成参数
        params = next(
            t["function"]["parameters"]
            for t in registry.get_openai_tools()
            if t["function"]["name"] == "mcp__fake__get_product_info"
        )
        assert params["type"] == "object"
        assert "sku" in params["properties"]

    async def test_source_labels_separate_mcp_from_native(self, adapter_factory, registry):
        """source 让「工具来自外部进程」在注册表层面可见（纯可观测性）。"""

        async def _native(arguments: dict) -> str:
            return "native"

        registry.register(
            name="native_readonly",
            description="native 只读工具",
            parameters={"type": "object", "properties": {}},
            handler=_native,
        )
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)

        assert registry.source_for("native_readonly") == SOURCE_NATIVE
        assert registry.source_for("mcp__fake__get_product_info") == SOURCE_MCP
        assert registry.tools_by_source(SOURCE_MCP) == registry.tools_by_source("mcp")
        assert "native_readonly" in registry.tools_by_source(SOURCE_NATIVE)
        assert "native_readonly" not in registry.tools_by_source(SOURCE_MCP)


# ===== 2. high-risk side effect：fail-closed policy =====


class TestHighRiskSideEffectPolicy:
    async def test_write_declared_server_registers_nothing(self, adapter_factory, registry):
        """server 声明 risk_level=high → 一个工具都不注册（只注册显式 low）。

        这是本仓对「外部不可信工具」的核心防线：写操作 MCP 工具**不进注册表**，
        因此不会出现在 Agent 的 Function Calling 列表里，也永远到不了 HITL 闸门。
        """
        adapter = adapter_factory(risk_level="high")
        before = _counter("mcp_tool_register_total", server="fake", outcome="skipped_not_low_risk")
        registered = await register_mcp_tools(registry, adapter)

        assert registered == []
        assert registry.list_tools() == []
        assert registry.get_openai_tools() == []
        after = _counter("mcp_tool_register_total", server="fake", outcome="skipped_not_low_risk")
        assert after - before == len(ALL_FAKE_TOOLS)

    async def test_write_tool_is_absent_from_function_calling_surface(
        self, adapter_factory, registry
    ):
        """即使 LLM「想」调用，也没有任何可达路径 —— 名字根本不存在。"""
        adapter = adapter_factory(risk_level="high")
        await register_mcp_tools(registry, adapter)
        assert "mcp__fake__issue_refund" not in registry.list_tools()
        assert "mcp__fake__issue_refund" not in registry.tools_by_source(SOURCE_MCP)
        # registry.execute 对未知工具返回错误字符串而不是抛异常
        out = await registry.execute("mcp__fake__issue_refund", {"order_id": "O-1"})
        assert "不存在" in out

    async def test_low_declared_server_still_exposes_the_write_tool(
        self, adapter_factory, registry
    ):
        """**已知缺口（NOT_IMPLEMENTED 的响应侧/逐工具风险）** —— 如实刻画，不掩盖。

        ``MCPServerConfig.risk_level`` 是**按 server** 声明的，``discover_tools`` 把它
        原样赋给该 server 的**每一个**工具。因此当一个 server 被显式声明为 ``low`` 时，
        它暴露的 ``issue_refund`` 会被当成只读工具注册下来（risk_level="low"）。

        也就是说：「只注册显式 low」这道防线的强度**取决于操作者是否把 server 正确
        声明为 low**，而 server 本身是不可信输入。本仓当前没有逐工具的风险声明 /
        校验，也没有在注册时交叉核对工具语义。

        这条断言的作用是**锁住当前真实行为**：一旦将来补上逐工具风险治理，这条测试
        会红，从而提醒同步更新设计文档，而不是让缺口悄悄固化。
        """
        adapter = adapter_factory(risk_level="low")
        registered = await register_mcp_tools(registry, adapter)

        assert "mcp__fake__issue_refund" in registered
        assert registry.risk_level_for("mcp__fake__issue_refund") == "low"
        assert registry.is_side_effect("mcp__fake__issue_refund") is False


# ===== 3. timeout =====


class TestTimeoutContract:
    async def test_timeout_raises_mcp_timeout_deterministically(self, adapter_factory, registry):
        """慢工具必须**稳定**收敛到 MCPTimeoutError。

        稳定性来自两侧的确定性设计：fake server 固定 sleep 30s（远大于 timeout），
        且 SDK 的 read timeout 被设成外层超时的 2 倍（``_SDK_READ_TIMEOUT_SLACK``），
        所以外层第一道防线必定先到期 —— 异常类型不会在两个超时来源之间摇摆。
        """
        adapter = adapter_factory(timeout_seconds=0.5)
        await register_mcp_tools(registry, adapter)

        for _ in range(3):
            with pytest.raises(MCPTimeoutError):
                await adapter.invoke("slow_lookup", {"q": "x"})

    async def test_timeout_increments_timeout_status_telemetry(self, adapter_factory):
        adapter = adapter_factory(timeout_seconds=0.5)
        before = _counter(
            "mcp_tool_call_total", server="fake", tool="slow_lookup", status="timeout"
        )
        with pytest.raises(MCPTimeoutError):
            await adapter.invoke("slow_lookup", {"q": "x"})
        after = _counter("mcp_tool_call_total", server="fake", tool="slow_lookup", status="timeout")
        assert after - before == 1

    @pytest.mark.timeout(60)
    async def test_adapter_closes_cleanly_after_a_timeout(self, adapter_factory):
        """超时之后 adapter 必须能干净关闭（不能卡死，也不能泄漏子进程）。

        这条不是凑数：``slow_lookup`` 会让 server 挂 30s，而 session 的 teardown 要
        穿过 SDK 的 anyio cancel scope ——「超时后关不掉」是本仓真实踩过的一类生命周期
        缺陷。

        已知边界（本条**不**声称覆盖）：

        - SDK 的 ``BaseSession.__aexit__`` 在**超时之后**的 teardown 中会取消调用方
          task，而 ``McpSdkClient.close`` / ``MCPToolAdapter.close`` 显式吞掉
          ``CancelledError``：cleanup 是 best-effort 的，绝不能把「关一个 MCP session」
          升级成「整个进程 shutdown 失败」。代价是**任何 asyncio 超时原语都包不住
          close**（``wait_for`` 会 await 被取消的 task 而 close 吞掉取消；换 task 执行
          则撞 cancel-scope ``RuntimeError``）。完整推导见本文件「teardown 硬上限」。
          本仓容器（``core/container.py::_close_mcp_tools``）直接 ``await close()``，
          不套任何超时原语，因此那条路径是安全的。
        - ``@pytest.mark.timeout`` 在**本仓当前依赖下是 no-op**（``pytest-timeout`` 未
          安装；``pyproject.toml`` 里该 marker 的说明本身也写了 "no-op if the plugin is
          absent"）。它只是将来装了插件后的第二层防线；**当前真正生效的是本条调用的
          :func:`_close_watched` 看门狗，以及 :class:`TestTimeoutTeardownBound` 里走进程外
          harness 的硬上限**。
        - ``_close_watched`` 是**尽力**上限，不是硬上限：它只保证"子进程不肯死"这一
          真实成因会被解开。close 若卡在与子进程无关的地方，它救不回来。无条件的保证
          由进程外 harness 提供（连"close 无视取消"都能物理消灭）。
        """
        adapter = adapter_factory(timeout_seconds=0.5)
        await adapter.connect()
        with pytest.raises(MCPTimeoutError):
            await adapter.invoke("slow_lookup", {"q": "x"})

        # 看门狗看守的关闭：预算内没自己关掉就是**失败**，而不是把整个套件挂住。
        outcome = await _close_watched(adapter, budget=CLOSE_WATCHDOG_SECONDS)
        assert outcome.clean, (
            f"close 未在 {CLOSE_WATCHDOG_SECONDS}s 内自己结束"
            f"（用时 {outcome.elapsed:.1f}s，杀掉了 {sorted(outcome.killed)}）"
        )
        assert _own_fake_servers() == set(), "干净关闭后不得残留 fake MCP server"


class TestTimeoutTeardownBound:
    """**整个 timeout lifecycle 的硬上限，含 teardown**（close 本身就是 teardown）。

    这一类不测适配器行为，只测"套件一定结束得了、且不留孤儿进程"这条基础设施
    性质。分三层：

    1. 进程外正常路径 —— 端到端跑完一个干净 lifecycle；
    2. 进程外 close/hang 回归 —— 构造 close 永不返回，断言硬上限照样生效；
    3. 进程内看门狗回归 —— 断言 teardown 用的那条路径能解开"子进程不肯死"。

    **本类的用例都不依赖 fixture**：它们自带子进程与自己的上限，所以在"整套 teardown
    已经坏掉"的世界里仍然跑得完、并且仍然给出结论。这一点是实测的，不是推断 ——
    把 fixture teardown 退回成裸 ``await adapter.close()``、并让 close 完全无视取消
    （也就是修复前的状态）之后，本文件里 59 条用例中的 58 条会卡死，而
    ``test_close_hang_cannot_stall_the_suite`` 仍然在 21s 内通过。

    反过来说，**进程内的用例（含 fixture teardown）在"全局 close 回归"面前是挂死的**，
    唯一能作恶的就是那个无视取消的 close —— 进程内没有任何原语能收拾它。所以这一类里
    真正提供保证的是**进程外那两条**；进程内的看门狗只把"子进程不肯死"这一种成因从
    挂死降级成"被杀干净 + 明确报错"。
    """

    #: 这些用例自己持上限，所以预算取小值：跑得快，失败信号也更早。
    HANG_BOUND_SECONDS = 20.0

    def test_clean_timeout_lifecycle_runs_end_to_end(self):
        """正常路径：connect → 超时 → 干净 close，全程在硬上限内跑完。"""
        result = _run_timeout_lifecycle(mode="clean", bound=self.HANG_BOUND_SECONDS)

        assert result.status == "completed", (
            f"lifecycle 未正常完成：status={result.status} "
            f"rc={result.returncode} events={result.events}\n{result.stderr[-2000:]}"
        )
        # 前提校验：子进程确实观测到超时、也确实起过一个 fake server。少了这两条，
        # 后面的断言会退化成"什么都没发生也算过"。
        assert result.saw("timeout_observed"), result.events
        assert result.reported_servers(), f"子进程没自报任何 server：{result.events}"
        assert result.saw("closed_cleanly"), result.events
        assert (
            result.survivors == frozenset()
        ), f"干净收尾后仍残留 fake MCP server: {sorted(result.survivors)}"

    def test_close_hang_cannot_stall_the_suite(self):
        """**回归用例**：close 永不返回时，硬上限照样让套件结束，且不留子进程。

        构造方式：``hang`` 模式把 ``close`` 换成 ``await Event().wait()`` —— 一个
        吞掉取消之后继续卡住的协程。进程内无解（``wait_for`` 与"到点放弃 task"两条路
        都会让 pytest-asyncio 收 loop 时挂死，见「teardown 硬上限」），所以放到子进程
        里，由父进程 ``killpg`` 物理收场。

        这条锁住三件事：套件**会**结束、结果**如实**标成 ``hard_killed``、fake MCP
        server **不残留**。
        """
        result = _run_timeout_lifecycle(mode="hang", bound=self.HANG_BOUND_SECONDS)

        # 1) 上限真的生效：子进程永远不返回，父进程必须在上限 + 宽限内收场。
        assert result.status == "hard_killed", (
            f"期望被硬上限杀掉，实际 status={result.status} rc={result.returncode} "
            f"events={result.events}\n{result.stderr[-2000:]}"
        )
        assert result.elapsed < self.HANG_BOUND_SECONDS + 15.0, result.elapsed
        # 2) 回归真的被构造出来了：子进程观测到超时、且确实有活着的 server。否则
        #    "没有残留"只是因为"本来就没有"，是空断言。
        assert result.saw("timeout_observed"), result.events
        assert result.reported_servers(), f"回归没构造出泄漏场景：{result.events}"
        assert not result.saw("closed_cleanly"), "close 不该返回"
        # 3) 硬上限必须把子进程连同它的 fake MCP server 一起带走。
        assert (
            result.survivors == frozenset()
        ), f"超时后残留 fake MCP server: {sorted(result.survivors)}"

    async def test_in_process_watchdog_unblocks_a_close_stuck_on_its_server(
        self, adapter_factory, monkeypatch
    ):
        """进程内 teardown 路径：close 卡在"等子进程死"上时，看门狗能把它解开。

        替身 close 一直等到自己那个 fake MCP server 真的死掉为止 —— 这就是"卡死成因 =
        子进程不肯死"的可控形状。于是 :func:`_close_watched` 的"到点 → 杀子进程"这条
        路径可以被**真实断言**，而不是靠注释；它又是同 task 执行的，不违反 SDK 的
        cancel-scope 契约。
        """
        adapter = adapter_factory(timeout_seconds=0.5)
        await adapter.connect()
        with pytest.raises(MCPTimeoutError):
            await adapter.invoke("slow_lookup", {"q": "x"})

        stuck_on = _own_fake_servers()
        assert stuck_on, "前提校验：此时应有一个活着的 fake MCP server"

        async def _close_until_the_server_dies() -> None:
            while any(_pid_alive(pid) for pid in stuck_on):
                await asyncio.sleep(0.02)

        monkeypatch.setattr(adapter, "close", _close_until_the_server_dies)

        # 预算故意压到远小于 SDK 自身的 teardown 花费，逼出"到点 → 杀子进程"这条分支。
        outcome = await _close_watched(adapter, budget=0.5, label="regression")

        assert not outcome.clean, "替身 close 是靠看门狗救回来的，clean 应当为 False"
        assert outcome.killed, "看门狗必须真的杀掉过 fake MCP server"
        assert _own_fake_servers() == set(), "看门狗之后不得残留 fake MCP server"

    async def test_normal_close_path_is_unaffected_by_the_watchdog(self, adapter_factory):
        """看门狗不得影响正常 close：健康路径既不误触发，也不杀任何进程。

        这是"加了防线"最容易写坏的地方 —— 预算取得比真实 teardown 短，健康路径就会
        被误判成 hang 然后被杀掉，测试反而变成绿的假象。
        """
        adapter = adapter_factory(timeout_seconds=0.5)
        await adapter.connect()
        before = _own_fake_servers()
        assert before, "前提校验：connect 之后应有一个活着的 fake MCP server"

        outcome = await _close_watched(adapter, budget=CLOSE_WATCHDOG_SECONDS)

        assert outcome.clean, f"正常 close 不该触发看门狗（杀掉了 {sorted(outcome.killed)}）"
        assert outcome.killed == frozenset()
        assert _own_fake_servers() == set()


# ===== 4. oversized =====


class TestOversizedContract:
    async def test_oversized_request_payload_rejected_before_send(self, adapter_factory):
        """请求侧上限是**真正生效**的防线：超限参数在发出前就被拒。

        注意限制的是**发出去**的请求（``max_payload_bytes``），不是返回值。
        """
        adapter = adapter_factory(max_payload_bytes=1024)
        with pytest.raises(MCPPayloadTooLargeError):
            await adapter.invoke("bulk_dump", {"pad": "x" * 4096})

    async def test_oversized_request_payload_telemetry(self, adapter_factory):
        adapter = adapter_factory(max_payload_bytes=1024)
        before = _counter(
            "mcp_tool_error_total", server="fake", tool="bulk_dump", reason="payload_too_large"
        )
        with pytest.raises(MCPPayloadTooLargeError):
            await adapter.invoke("bulk_dump", {"pad": "x" * 4096})
        after = _counter(
            "mcp_tool_error_total", server="fake", tool="bulk_dump", reason="payload_too_large"
        )
        assert after - before == 1

    async def test_oversized_result_passes_through_unbounded(self, adapter_factory, registry):
        """**已知缺口**：响应体积当前**不受任何限制** —— 如实刻画，不掩盖。

        ``MCPToolAdapter.invoke`` 只在**发送前**检查 payload 大小，返回值直接经
        ``_normalize_result`` 变成 Agent 上下文里的一段文本。一个恶意的 / 失控的
        MCP server 可以返回任意大的结果并把它灌进上下文与 Token 预算。

        本仓的兜底不在适配器里，而在下游：``core/tool_result_*`` 的 Tool Result
        Context Budget（截断 / 压缩 / offload）。也就是说当前依赖的是**通用**防线，
        而不是 MCP 边界自身的限制。

        与上一条同理，这条断言锁住当前真实行为：补上响应侧上限后它会红。
        """
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)

        big = await registry.execute("mcp__fake__bulk_dump", {"size": 200_000})
        assert len(big) == 200_000  # 全量透传，没有被适配器截断


# ===== 5. failing =====


class TestFailingToolContract:
    async def test_server_reported_failure_raises_tool_execution_error(self, adapter_factory):
        """server 明确说 isError → MCPToolExecutionError(reason="tool_error")。"""
        adapter = adapter_factory()
        with pytest.raises(MCPToolExecutionError) as excinfo:
            await adapter.invoke("flaky_lookup", {"code": "E_DOWNSTREAM"})
        assert excinfo.value.reason == "tool_error"
        assert "E_DOWNSTREAM" in str(excinfo.value)

    async def test_failing_tool_error_telemetry_keeps_reason(self, adapter_factory):
        adapter = adapter_factory()
        before = _counter(
            "mcp_tool_error_total", server="fake", tool="flaky_lookup", reason="tool_error"
        )
        with pytest.raises(MCPToolExecutionError):
            await adapter.invoke("flaky_lookup", {"code": "E_DOWNSTREAM"})
        after = _counter(
            "mcp_tool_error_total", server="fake", tool="flaky_lookup", reason="tool_error"
        )
        assert after - before == 1

    async def test_via_registry_failure_degrades_to_user_safe_message(
        self, adapter_factory, registry
    ):
        """经 ToolRegistry 执行时，MCP 异常被降级为一句用户可读的话，而不是把
        内部异常类型 / 远端错误细节抛进 Agent 上下文。"""
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)
        out = await registry.execute("mcp__fake__flaky_lookup", {"code": "E_DOWNSTREAM"})
        assert out == "工具 'mcp__fake__flaky_lookup' 执行失败，请稍后重试"
        # 远端错误细节不得泄漏到 Agent 可见的返回值里
        assert "E_DOWNSTREAM" not in out


# ===== allowlist（policy 的一环）=====


class TestAllowlistPolicy:
    async def test_unlisted_tool_is_never_registered(self, adapter_factory, registry):
        adapter = adapter_factory(allowed_tools=("get_product_info",))
        registered = await register_mcp_tools(registry, adapter)
        assert registered == ["mcp__fake__get_product_info"]
        assert "mcp__fake__secret_admin_tool" not in registry.list_tools()

    async def test_invoking_unlisted_tool_raises_unauthorized(self, adapter_factory):
        adapter = adapter_factory(allowed_tools=("get_product_info",))
        with pytest.raises(MCPUnauthorizedToolError):
            await adapter.invoke("secret_admin_tool", {})

    async def test_unlisted_tool_name_is_not_used_as_metric_label(self, adapter_factory):
        """被拒绝的远端工具名不得成为 label（否则 server 一改工具名就能无界扩张时序）。"""
        adapter = adapter_factory(allowed_tools=("get_product_info",))
        before_star = _counter(
            "mcp_tool_error_total", server="fake", tool="*", reason="not_allowed"
        )
        with pytest.raises(MCPUnauthorizedToolError):
            await adapter.invoke("secret_admin_tool", {})
        after_star = _counter("mcp_tool_error_total", server="fake", tool="*", reason="not_allowed")
        assert after_star - before_star >= 1

        from prometheus_client import REGISTRY

        leaked = [
            sample
            for metric in REGISTRY.collect()
            for sample in metric.samples
            if sample.name == "mcp_tool_error_total"
            and "secret_admin_tool" in sample.labels.get("tool", "")
        ]
        assert leaked == []


# ===== 进程外 harness 的子进程入口 =====
#
# 硬上限只能由"能杀掉子进程的那一方"来执行，所以被测 lifecycle 跑在一个
# ``start_new_session=True`` 的子进程里，由 :func:`_run_timeout_lifecycle` 看护。
# 这不是把断言搬去别处：断言仍然在本文件里，只是**执行**被隔离了。

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--lifecycle":
        raise SystemExit(_driver_main(sys.argv[2]))
    raise SystemExit(
        "本文件只能由 pytest 收集，或作为 lifecycle 子进程入口使用："
        "python -m tests.integration.test_mcp_contract_e2e --lifecycle {clean|hang}"
    )


# ===== policy：注册表声明与 HITL 闸门一致 =====


class TestRiskPolicyConsistency:
    async def test_registered_read_tools_are_low_risk_and_never_need_approval(
        self, adapter_factory, registry
    ):
        """只读 MCP 工具显式 low + side_effect=False ⇒ HITL 闸门永不拦它。

        这条是「注册表声明」与「审批策略」的一致性断言：``classify_risk`` 的优先级是
        显式声明 > 工具名白名单 > 金额阈值，所以显式 low 同时也保证了一个带
        ``amount=999999`` 的只读查询不会被金额阈值误判成 HIGH 而白占审批队列。
        """
        from core.hitl.risk import RiskLevel, classify_risk, requires_approval

        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)
        name = "mcp__fake__get_product_info"

        assert registry.risk_level_for(name) == "low"
        assert registry.is_side_effect(name) is False
        assert registry.cache_policy_for(name).enabled is False

        level = classify_risk(
            name, explicit=registry.risk_level_for(name), arguments={"sku": "S", "amount": 999999}
        )
        assert level == RiskLevel.LOW
        assert requires_approval(level) is False

    async def test_unknown_tool_is_not_treated_as_low_risk(self, registry):
        """未注册工具必须返回 None（「不知道」）而不是 low（「安全」）。"""
        assert registry.risk_level_for("mcp__nope__nope") is None
        assert registry.source_for("mcp__nope__nope") is None
        assert registry.tools_by_source("nonexistent-source") == []


class TestDefaultHighRiskPolicy:
    """ "未声明就不是低风险" —— 本仓对外部工具的核心立场。

    漏配 / 配错 ``risk_level`` 的后果必须是「工具不注册」，绝不能是「默认按只读
    放行」。否则一个拼错的配置就能让整台 server 的工具静默拿到 ``risk_level=low``，
    从而绕过 HITL 闸门的显式分级。
    """

    def test_default_risk_level_is_high(self):
        from core.hitl.risk import RiskLevel
        from tools.mcp_adapter import DEFAULT_RISK_LEVEL

        assert DEFAULT_RISK_LEVEL is RiskLevel.HIGH

    def test_unconfigured_server_defaults_to_high(self):
        from core.hitl.risk import RiskLevel
        from tools.mcp_adapter import MCPServerConfig

        cfg = MCPServerConfig(name="s", command="c")
        assert cfg.risk_level is RiskLevel.HIGH

    @pytest.mark.parametrize("bad", ["read", "write", "readonly", "safe", "LOW-ish", "?", ""])
    def test_invalid_risk_level_only_converges_upward(self, bad):
        """非法值只能向上收敛到 HIGH —— 绝不回落成 LOW/MEDIUM。

        特别包含历史词汇 ``read`` / ``write``：它们在 ``RiskLevel`` 里不存在，
        若被当成合法值接住就等于退回"外部工具默认只读"的老行为。
        """
        from core.hitl.risk import RiskLevel
        from tools.mcp_adapter import load_mcp_server_configs

        cfgs = load_mcp_server_configs(
            f'[{{"name":"s","command":"c","allowed_tools":["t"],"risk_level":"{bad}"}}]'
        )
        assert len(cfgs) == 1
        assert cfgs[0].risk_level is RiskLevel.HIGH

    @pytest.mark.parametrize("raw,expected", [("low", "low"), ("LOW", "low")])
    def test_explicit_low_is_honoured(self, raw, expected):
        from tools.mcp_adapter import load_mcp_server_configs

        cfgs = load_mcp_server_configs(
            f'[{{"name":"s","command":"c","allowed_tools":["t"],"risk_level":"{raw}"}}]'
        )
        assert cfgs[0].risk_level.value == expected

    def test_bare_string_low_is_normalised_to_risklevel(self):
        """``RiskLevel`` 是 ``str`` Enum：``"low" == RiskLevel.LOW`` 但不是同一对象。

        若不做归一，下游 ``is not RiskLevel.LOW`` 的身份比较会在传入裸字符串时
        静默失效，把工具误判成"非只读"而全部跳过。
        """
        from core.hitl.risk import RiskLevel
        from tools.mcp_adapter import MCPServerConfig

        assert MCPServerConfig(name="s", command="c", risk_level="low").risk_level is (
            RiskLevel.LOW
        )

    async def test_undeclared_server_registers_nothing(self, adapter_factory, registry):
        adapter = adapter_factory(risk_level="high")
        assert await register_mcp_tools(registry, adapter) == []
        assert registry.list_tools() == []

    async def test_medium_risk_server_registers_nothing(self, adapter_factory, registry):
        adapter = adapter_factory(risk_level="medium")
        assert await register_mcp_tools(registry, adapter) == []
        assert registry.list_tools() == []

    async def test_low_risk_server_registers_its_tools(self, adapter_factory, registry):
        adapter = adapter_factory(risk_level="low")
        registered = await register_mcp_tools(registry, adapter)
        assert registered
        assert registry.risk_level_for(registered[0]) == "low"


class TestServerAnnotationsAreNotTrusted:
    """MCP server 自述的 ``annotations`` 不能覆盖本地风险策略。

    第三方只要在自己的代码里加 ``readOnlyHint: true`` / ``destructiveHint: false``，
    就能"自称安全"。适配器故意不读 annotations，因此风险只由本地配置决定。
    """

    async def test_annotations_do_not_change_the_local_risk_level(self, adapter_factory, registry):
        adapter = adapter_factory(risk_level="high")
        specs = await adapter.discover_tools()
        assert specs, "fake server 至少暴露一个工具，断言才有意义"
        # 前提校验：fake server 确实在 wire 上宣称"只读 / 非破坏性"。
        # 没有这一步，"风险没被降低"可能只是因为 annotations 根本没送到，
        # 测试就变成一条永远为真的空断言。
        raw = await adapter._require_client().list_tools()
        assert any(
            getattr(t, "annotations", None) for t in raw
        ), "fake server 未发出 annotations：req-6 断言会变成空断言"
        # server 即使自述只读，本地判定仍是 HIGH。
        assert all(s.risk_level is RiskLevel.HIGH for s in specs)
        assert await register_mcp_tools(registry, adapter) == []

    async def test_annotations_do_not_block_an_explicitly_low_server(
        self, adapter_factory, registry
    ):
        """反向：annotations 也不是"放行"的额外门槛之外的干扰项。

        显式声明 ``low`` 的 server 照常注册，证明判定链路只读本地配置。
        """
        adapter = adapter_factory(risk_level="low")
        assert await register_mcp_tools(registry, adapter) != []


# ===== config 层 fail-closed（纯函数，无 IO）=====


class TestConfigFailClosed:
    def test_disabled_by_default(self):
        from core.config import validate_mcp_settings

        assert validate_mcp_settings(enabled=False, servers="") == []

    def test_enabled_without_allowlist_is_rejected(self):
        from core.config import validate_mcp_settings

        errors = validate_mcp_settings(enabled=True, servers="   ")
        assert errors and "MCP_SERVERS" in errors[0]

    @pytest.mark.parametrize(
        "servers",
        ["{not json", '{"name": "x"}', "[]", '[{"transport": "stdio"}]'],
    )
    def test_structurally_invalid_allowlist_is_rejected(self, servers):
        from core.config import validate_mcp_settings

        assert validate_mcp_settings(enabled=True, servers=servers) != []

    def test_valid_allowlist_passes(self):
        from core.config import validate_mcp_settings

        servers = (
            '[{"name":"fake","transport":"stdio","command":"python3",'
            '"args":["-m","srv"],"allowed_tools":["get_product_info"],'
            '"risk_level":"low"}]'
        )
        assert validate_mcp_settings(enabled=True, servers=servers) == []

    def test_missing_risk_level_is_reported_at_startup(self):
        """漏配 risk_level 必须在**启动期**就报出来，而不是等到工具列表为空才发现。"""
        from core.config import validate_mcp_settings

        servers = (
            '[{"name":"fake","transport":"stdio","command":"python3",'
            '"args":["-m","srv"],"allowed_tools":["get_product_info"]}]'
        )
        problems = validate_mcp_settings(enabled=True, servers=servers)
        assert any("未显式声明 risk_level" in p for p in problems), problems

    @pytest.mark.parametrize("bad", ["read", "write", "safe", "???"])
    def test_invalid_risk_level_is_reported_at_startup(self, bad):
        """旧词汇 read/write 与笔误都必须被指出，而不是被静默当成合法值。"""
        from core.config import validate_mcp_settings

        servers = (
            f'[{{"name":"fake","transport":"stdio","command":"python3",'
            f'"args":["-m","srv"],"allowed_tools":[],"risk_level":"{bad}"}}]'
        )
        problems = validate_mcp_settings(enabled=True, servers=servers)
        assert any("risk_level 非法" in p for p in problems), problems

    def test_config_parser_rejects_unknown_transport(self):
        from tools.mcp_adapter import load_mcp_server_configs

        configs = load_mcp_server_configs(
            '[{"name":"x","transport":"carrier-pigeon","command":"c"}]'
        )
        assert configs == []

    def test_build_adapters_skips_disabled_servers(self):
        from tools.mcp_adapter import load_mcp_server_configs

        configs = load_mcp_server_configs(
            '[{"name":"on","command":"c","enabled":true},'
            '{"name":"off","command":"c","enabled":false}]'
        )
        adapters = build_mcp_adapters(configs)
        assert [a.config.name for a in adapters] == ["on"]


# ===== telemetry：happy path 计数 =====


class TestTelemetryContract:
    async def test_successful_call_increments_ok_status(self, adapter_factory, registry):
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)
        before = _counter(
            "mcp_tool_call_total", server="fake", tool="get_product_info", status="ok"
        )
        await registry.execute("mcp__fake__get_product_info", {"sku": "SKU-1"})
        after = _counter("mcp_tool_call_total", server="fake", tool="get_product_info", status="ok")
        assert after - before == 1

    async def test_register_outcome_counter(self, adapter_factory, registry):
        before = _counter("mcp_tool_register_total", server="fake", outcome="registered")
        adapter = adapter_factory()
        await register_mcp_tools(registry, adapter)
        after = _counter("mcp_tool_register_total", server="fake", outcome="registered")
        assert after - before == len(ALL_FAKE_TOOLS)

    def test_metrics_do_not_use_high_cardinality_labels(self):
        """MCP 指标的 label 只允许 server/tool/status/reason/outcome 这类低基数维度。"""
        from prometheus_client import REGISTRY

        forbidden = {"run_id", "thread_id", "user_id", "query", "session_id", "error_message"}
        offenders = [
            (sample.name, set(sample.labels))
            for metric in REGISTRY.collect()
            if metric.name in {"mcp_tool", "mcp_tool_error", "mcp_tool_register"}
            for sample in metric.samples
            if forbidden & set(sample.labels)
        ]
        assert offenders == []
