"""
化妆品领域种子数据（v5.0）
从 JSON 文件加载 RAG 知识库初始化数据。

数据文件位于 data/seed/ 目录：
- product_knowledge.json — 产品成分与功效知识（50 条）
- faq.json — 客服常见问答（45 条）
- tech_support.json — 技术支持文档（35 条）
- complaint_knowledge.json — 投诉处理知识（38 条）
"""

import json
import os

from core.logger import get_logger

logger = get_logger("seed_data")

# 种子数据目录
_SEED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "seed"
)

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
    with open(filepath, encoding="utf-8") as f:
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
    seed_image_knowledge(kb)  # v5.1: 图片知识库


def seed_image_knowledge(kb, collection_name="image_knowledge"):
    """
    v5.1: 图片知识库种子数据（CLIP 多模态检索）
    从 data/seed/image_knowledge.json 加载图片元数据。
    注意：实际图片文件需放在 data/images/ 目录下。
    """
    items = _load_seed_file("image_knowledge.json")
    if not items:
        return

    image_paths = [item["image_path"] for item in items]
    metadatas = [item.get("metadata", {}) for item in items]

    # v6.1: QdrantKnowledgeBase 可能不支持 add_image_documents，优雅降级
    if hasattr(kb, "add_image_documents"):
        kb.add_image_documents(collection_name, image_paths, metadatas)
        logger.info(f"图片种子数据加载完成: {collection_name} ({len(items)} 条)")
    else:
        logger.warning(f"当前知识库不支持 add_image_documents（Qdrant 未实现 CLIP 多模态检索），跳过图片种子数据")
