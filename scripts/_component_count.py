#!/usr/bin/env python3
"""DI 容器组件计数辅助脚本。

由 Makefile component-count 目标调用，避免 Makefile 内联 Python 的 tab 缩进问题。
"""

import asyncio
import os
import sys

sys.path.insert(0, ".")
os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("QDRANT_HOST", "localhost")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from core.container import ServiceContainer  # noqa: E402


async def main():
    c = ServiceContainer()
    try:
        await asyncio.wait_for(c.initialize(), timeout=5.0)
    except asyncio.TimeoutError:
        print("  ⚠️ 容器初始化超时（部分服务未运行），已注册组件:")
    except Exception as e:
        print(f"  ⚠️ 容器初始化异常: {e}")

    if hasattr(c, "_services") and c._services:
        for name in sorted(c._services):
            print(f"    ✓ {name}")
        print(f"  --- 总计: {len(c._services)} 个组件 ---")
    else:
        svc_count = sum(
            1 for x in dir(c) if not x.startswith("_") and not callable(getattr(c, x, lambda: None))
        )
        print(f"  服务组件（自动检测）: {svc_count}")


if __name__ == "__main__":
    asyncio.run(main())
