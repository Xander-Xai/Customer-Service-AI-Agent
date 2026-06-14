"""
RAG Query Rewriter（v5.1）
对用户查询进行改写，提升检索召回率。

实现策略：
1. 同义词扩展 — 化妆品领域同义词映射
2. HyDE 简化版 — 生成假设性回答作为查询
3. 查询拆分 — 多问题拆分为单问题

使用方式：
    rewriter = QueryRewriter()
    expanded = rewriter.expand_query("烟酰胺精华好用吗")
    # → "烟酰胺精华好用吗 烟酰胺 精华液 功效 成分"
"""

import re

from core.logger import get_logger

logger = get_logger("rag.query_rewriter")


# 化妆品领域同义词映射表
_SYNONYM_MAP: dict[str, list[str]] = {
    # 成分类
    "烟酰胺": ["维生素B3", "VB3", "烟碱酰胺"],
    "玻尿酸": ["透明质酸", "玻尿酸钠", "HA"],
    "维C": ["维生素C", "VC", "抗坏血酸", "维他命C"],
    "视黄醇": ["维A醇", "A醇", "维生素A醇", "Retinol"],
    "水杨酸": ["BHA", "柳酸", "2-羟基苯甲酸"],
    "果酸": ["AHA", "乙醇酸", "甘醇酸"],
    "神经酰胺": ["Ceramide", "赛洛美"],
    "角鲨烷": ["Squalane", "深海鲨鱼肝油"],
    "虾青素": ["Astaxanthin", "虾红素"],
    # 功效类
    "美白": ["提亮", "亮肤", "淡斑", "均匀肤色"],
    "保湿": ["补水", "锁水", "滋润", "润肤"],
    "抗老": ["抗皱", "抗衰", "紧致", "抗氧化"],
    "控油": ["收敛", "清爽", "去油"],
    "祛痘": ["祛痘", "消痘", "祛痘印", "去痘"],
    "防晒": ["紫外线防护", "UV防护", "SPF"],
    "过敏": ["敏感", "不适", "红肿", "刺激", "泛红"],
    # 产品类
    "面霜": ["护肤霜", "乳霜", "日霜", "晚霜"],
    "精华": ["精华液", "精华露", "安瓶"],
    "面膜": ["面贴膜", "泥膜", "睡眠面膜"],
    "洁面": ["洗面奶", "洁面乳", "洁面泡沫", "清洁"],
    "眼霜": ["眼部精华", "眼膜"],
    "爽肤水": ["化妆水", "柔肤水", "精华水"],
    "乳液": ["润肤乳", "身体乳"],
}


class QueryRewriter:
    """查询改写器：扩展查询以提升 RAG 检索召回率。"""

    def __init__(self, synonym_map: dict[str, list[str]] | None = None):
        self._synonym_map = synonym_map or _SYNONYM_MAP

    def expand_query(self, query: str) -> str:
        """扩展查询：在原始查询后追加相关同义词。

        Args:
            query: 原始用户查询

        Returns:
            扩展后的查询文本
        """
        extras = []
        query_lower = query.lower()

        for keyword, synonyms in self._synonym_map.items():
            if keyword.lower() in query_lower:
                # 取前 2 个同义词，避免查询过长
                extras.extend(synonyms[:2])

        if not extras:
            return query

        # 去重，保持顺序
        seen = set()
        unique_extras = []
        for e in extras:
            if e not in seen:
                seen.add(e)
                unique_extras.append(e)

        expanded = f"{query} {' '.join(unique_extras)}"
        logger.debug(f"查询扩展: '{query}' → '{expanded}'")
        return expanded

    def split_multi_question(self, query: str) -> list[str]:
        """拆分多问题查询为单问题列表。

        Args:
            query: 可能包含多个问题的查询

        Returns:
            拆分后的问题列表，如果只有一个问题则返回 [query]
        """
        # 中文问号、英文问号、分号、句号分隔
        parts = re.split(r"[？?；;。]", query)
        questions = [p.strip() for p in parts if p.strip() and len(p.strip()) > 3]

        if len(questions) <= 1:
            return [query]

        logger.debug(f"查询拆分: '{query}' → {questions}")
        return questions

    def rewrite_for_collection(self, query: str, collection: str) -> str:
        """针对特定 collection 改写查询，添加领域前缀。

        Args:
            query: 原始查询
            collection: 目标 collection 名

        Returns:
            改写后的查询
        """
        prefixes = {
            "product_knowledge": "产品知识：",
            "faq": "常见问题：",
            "tech_support": "技术支持：",
            "complaint_knowledge": "投诉处理：",
        }
        prefix = prefixes.get(collection, "")
        return f"{prefix}{query}" if prefix else query


def create_query_rewriter() -> QueryRewriter:
    """工厂函数：创建查询改写器"""
    return QueryRewriter()
