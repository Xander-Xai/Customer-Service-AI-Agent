"""
v6.0: 默认实现已迁移至 Qdrant。
保持此文件向后兼容——对齐 QdrantKnowledgeBase API。
"""

from rag.qdrant_knowledge_base import QdrantKnowledgeBase

CosmeticsKnowledgeBase = QdrantKnowledgeBase
__all__ = ["CosmeticsKnowledgeBase"]
