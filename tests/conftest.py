"""
pytest conftest — 环境补丁 + 默认 lane 出网守卫

1. 修复 asyncio.to_thread / run_in_executor 在容器环境中挂起的问题
2. 默认测试 lane 的出网守卫（issue #52）：`make test` 承诺离线，因此任何指向
   非回环地址的 connect 都视为违规。实现见 ``tests/offline_guard.py``。
"""

import asyncio
import concurrent.futures
import os
import sys

import pytest

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests import offline_guard  # noqa: E402


# ── asyncio.to_thread 补丁 ──
# 在容器环境中，共享 ThreadPoolExecutor 可能导致 to_thread 挂起。
async def _patched_to_thread(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        return await loop.run_in_executor(executor, lambda: func(*args, **kwargs))
    finally:
        executor.shutdown(wait=False)


asyncio.to_thread = _patched_to_thread

# ── run_in_executor 补丁 ──
# 补丁 BaseEventLoop.run_in_executor 以确保使用显式 executor
_original_run_in_executor = asyncio.BaseEventLoop.run_in_executor


def _patched_run_in_executor(self, executor, func, *args):
    """保持 run_in_executor 的原始同步签名，避免 pytest/Starlette 卡死。"""
    if executor is None:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        future = _original_run_in_executor(self, executor, func, *args)

        def _shutdown_executor(_future):
            executor.shutdown(wait=False)

        future.add_done_callback(_shutdown_executor)
        return future
    return _original_run_in_executor(self, executor, func, *args)


asyncio.BaseEventLoop.run_in_executor = _patched_run_in_executor


# ── 默认 lane 出网守卫（issue #52）──


def _marked_for_network(node) -> bool:
    """node 自身或任一祖先（class/module）带 ``real_llm`` 标记 → 允许出网。"""
    marker = offline_guard.NETWORK_MARKER
    current = node
    while current is not None:
        try:
            if current.get_closest_marker(marker) is not None:
                return True
        except AttributeError:  # 非 Item（例如 Session）
            break
        current = getattr(current, "parent", None)
    return False


@pytest.fixture(autouse=True)
def _enforce_offline_lane(request):
    """默认 lane 禁止连接非回环地址；``real_llm`` lane 放行。"""
    if offline_guard.allow_network_env() or _marked_for_network(request.node):
        yield
        return
    offline_guard.install(owner=request.node.nodeid)
    try:
        yield
    finally:
        offline_guard.uninstall()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """违规即整轮失败 —— 即使抛出的异常被应用代码吞掉。

    单靠抛异常不够：``ApiReranker`` 等路径有 ``except Exception`` 会把传输层
    异常降级成一次「provider 失败」，用例仍然绿，但出网已经发生。这里按记录判定，
    堵掉这个「假绿」通道。判定逻辑本身见 ``offline_guard.report``（可单测）。
    """
    offline_guard.uninstall()
    text = offline_guard.report()
    if text is None:
        return
    print(text, flush=True)
    if exitstatus == 0:
        session.exitstatus = 1
