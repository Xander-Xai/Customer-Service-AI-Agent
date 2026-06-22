#!/usr/bin/env python3
"""
缓存预热脚本（v5.4）

通过 HTTP API 预热通用高频问题的缓存。
仅预热不依赖用户身份的通用问题（产品信息、使用方法、FAQ 等）。

用法:
  python scripts/warm_cache.py http://localhost:8000   # 命令行运行
  在 lifespan 中调用 warm_cache_via_api()              # 启动时自动执行
"""

import asyncio
import sys
import time

# 通用高频问题列表（不依赖用户身份）
WARM_QUERIES = [
    # 产品信息
    "请问你们的精华液含有哪些主要成分？适合敏感肌使用吗？",
    "面霜和精华液的正确使用顺序是什么？",
    "玻尿酸面膜多久用一次？",
    "你们有哪些适合油性皮肤的产品？",
    "烟酰胺美白霜的主要功效是什么？",
    # 使用方法
    "洁面乳的正确使用方法是什么？",
    "面膜敷多长时间最合适？",
    "护肤品使用顺序是怎样的？",
    # 退换货/售后
    "退换货政策是什么？",
    "产品过敏可以退货吗？",
    "如何申请退款？",
    # 常见 FAQ
    "你们的客服工作时间是什么？",
    "如何成为VIP会员？",
    "积分兑换规则是什么？",
]


async def warm_cache_via_api(base_url: str = "http://localhost:8000", max_concurrent: int = 2):
    """通过 HTTP API 预热缓存：逐条发送请求，利用服务端缓存机制

    Args:
        base_url: 服务地址
        max_concurrent: 最大并发数（避免打爆 LLM API）
    """
    import logging

    import httpx

    logger = logging.getLogger("warm_cache")
    url = f"{base_url}/api/chat"
    warmed = 0
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _warm_one(client: httpx.AsyncClient, query: str):
        nonlocal warmed
        async with semaphore:
            try:
                resp = await client.post(url, json={"query": query}, timeout=60.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("response"):
                        warmed += 1
                        logger.debug(f"  ✓ {query[:30]}... (cached={data.get('cached', False)})")
                else:
                    logger.warning(f"  ✗ {query[:30]}... HTTP {resp.status_code}")
            except Exception as e:
                logger.warning(f"  ✗ {query[:30]}... 失败: {e}")

    start = time.time()
    async with httpx.AsyncClient(trust_env=False) as client:
        await asyncio.gather(*[_warm_one(client, q) for q in WARM_QUERIES])

    elapsed = time.time() - start
    logger.info(f"缓存预热完成：{warmed}/{len(WARM_QUERIES)} 条成功，耗时 {elapsed:.1f}s")
    return warmed


if __name__ == "__main__":
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    asyncio.run(warm_cache_via_api(base_url))
