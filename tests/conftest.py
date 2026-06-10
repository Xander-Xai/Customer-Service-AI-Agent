"""
pytest conftest — 环境补丁
修复 asyncio.to_thread 和 run_in_executor 在容器环境中挂起的问题
"""
import asyncio
import concurrent.futures
import os
import sys

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


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


async def _patched_run_in_executor(self, executor, func, *args):
    if executor is None:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            return await _original_run_in_executor(self, executor, func, *args)
        finally:
            executor.shutdown(wait=False)
    return await _original_run_in_executor(self, executor, func, *args)


asyncio.BaseEventLoop.run_in_executor = _patched_run_in_executor
