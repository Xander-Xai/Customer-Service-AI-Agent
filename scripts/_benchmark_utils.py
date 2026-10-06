#!/usr/bin/env python3
"""共享的评测集查询加载器。

从 tests/eval/rag_benchmark.json 加载查询，供所有 benchmark 脚本复用。
评测集不存在时回退到硬编码查询。

用法:
    from scripts._benchmark_utils import load_eval_queries
    queries = load_eval_queries()
"""

import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
_EVAL_PATH = _PROJECT_ROOT / "tests" / "eval" / "rag_benchmark.json"

# 硬编码回退查询（30 条）
HARDCODED_FALLBACK: list[str] = [
    "烟酰胺有什么功效？",
    "敏感肌可以用视黄醇吗？",
    "透明质酸是什么？",
    "油性皮肤适合用什么面霜？",
    "VC精华不能和什么一起用？",
    "传明酸能祛斑吗？",
    "果酸和水杨酸有什么区别？",
    "神经酰胺对皮肤屏障有什么作用？",
    "防晒霜物理防晒和化学防晒怎么选？",
    "维诺雅有哪些美白产品？",
    "你们发什么快递？多久能到？",
    "怎么退货？退货流程是什么？",
    "会员有什么等级？各等级权益？",
    "支持什么付款方式？",
    "产品保质期多久？开封后能用多长时间？",
    "怎么辨别产品是不是正品？",
    "孕期可以用你们的产品吗？",
    "怎么开发票？",
    "积分怎么用？怎么兑换？",
    "企业采购有优惠吗？",
    "用了产品过敏了怎么办？",
    "护肤品的正确使用顺序是什么？",
    "夏天护肤和冬天护肤有什么不同？",
    "黑头怎么去除？",
    "医美手术后怎么护理皮肤？",
    "敏感肌应该怎么护肤？",
    "面膜多久敷一次比较好？",
    "成分之间有冲突吗？哪些不能一起用？",
    "不同年龄段应该怎么选择护肤品？",
    "运动前后需要护肤吗？",
]


def load_eval_queries(min_count: int = 100) -> list[str]:
    """从 RAG 评测集加载查询。

    Args:
        min_count: 评测集查询数低于此值时回退到硬编码查询。

    Returns:
        查询字符串列表。
    """
    if not _EVAL_PATH.exists():
        print(
            f"  WARNING: 评测集未找到 ({_EVAL_PATH})，回退到 {len(HARDCODED_FALLBACK)} 条硬编码查询"
        )
        return HARDCODED_FALLBACK.copy()

    try:
        with open(_EVAL_PATH, encoding="utf-8") as f:
            data = json.load(f)
        queries = [q["query"] for q in data.get("queries", [])]
        if len(queries) >= min_count:
            print(f"  从评测集加载 {len(queries)} 条查询")
            return queries
        else:
            print(f"  WARNING: 评测集只有 {len(queries)} 条（期望 ≥{min_count}），回退到硬编码查询")
            return HARDCODED_FALLBACK.copy()
    except Exception as e:
        print(f"  WARNING: 评测集加载失败: {e}，回退到硬编码查询")
        return HARDCODED_FALLBACK.copy()
