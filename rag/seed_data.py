"""
化妆品领域种子数据（v5.0）
从 JSON 文件加载 RAG 知识库初始化数据。

数据文件位于 data/seed/ 目录：
- product_knowledge.json — 产品成分与功效知识（50 条）
- faq.json — 客服常见问答（45 条）
- tech_support.json — 技术支持文档（35 条）
- complaint_knowledge.json — 投诉处理知识（38 条）
"""
import os
import json
from logger import get_logger

logger = get_logger("seed_data")

# 种子数据目录
_SEED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "seed")

# 收集名称 → JSON 文件映射
_SEED_FILES = {
    "product_knowledge": "product_knowledge.json",
    "faq": "faq.json",
    "tech_support": "tech_support.json",
    "complaint_knowledge": "complaint_knowledge.json",
}


def _load_seed_file(filename: str) -> list:
    """从 JSON 文件加载种子数据"""
    filepath = os.path.join(_SEED_DIR, filename)
    if not os.path.exists(filepath):
        logger.warning(f"种子数据文件不存在: {filepath}")
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def _seed_collection(kb, collection_name: str):
    """加载单个集合的种子数据到知识库"""
    filename = _SEED_FILES.get(collection_name)
    if not filename:
        logger.warning(f"未知的集合名称: {collection_name}")
        return

    items = _load_seed_file(filename)
    if not items:
        return

    documents = [item["content"] for item in items]
    metadatas = [item["metadata"] for item in items]
    ids = [item["id"] for item in items]

    kb.add_documents(collection_name, documents, metadatas, ids)
    logger.info(f"种子数据加载完成: {collection_name} ({len(items)} 条)")


def seed_product_knowledge(kb, collection_name="product_knowledge"):
    """产品成分与功效知识库"""
    _seed_collection(kb, collection_name)


def seed_faq(kb, collection_name="faq"):
    """客服常见问答"""
    _seed_collection(kb, collection_name)


def seed_tech_support(kb, collection_name="tech_support"):
    """技术支持文档"""
    _seed_collection(kb, collection_name)


def seed_complaint_knowledge(kb, collection_name="complaint_knowledge"):
    """投诉处理知识"""
    _seed_collection(kb, collection_name)


def seed_supplementary_data(kb):
    """补充数据：调用所有种子函数"""
    seed_product_knowledge(kb)
    seed_faq(kb)
    seed_tech_support(kb)
    seed_complaint_knowledge(kb)
