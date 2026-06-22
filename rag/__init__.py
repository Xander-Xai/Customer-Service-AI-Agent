"""RAG 检索增强生成模块"""

# v6.0: 默认导出 Qdrant 实现
from .qdrant_knowledge_base import QdrantKnowledgeBase

CosmeticsKnowledgeBase = QdrantKnowledgeBase

__all__ = ["CosmeticsKnowledgeBase"]
