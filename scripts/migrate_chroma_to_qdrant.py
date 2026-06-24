#!/usr/bin/env python3
"""
ChromaDB → Qdrant 数据迁移脚本（v6.0）

将 ChromaDB 持久化目录中的所有数据迁移到 Qdrant 实例。

用法:
    python3 scripts/migrate_chroma_to_qdrant.py \
        --chroma-dir ./chroma_db \
        --qdrant-host localhost \
        --qdrant-port 6333

前置条件:
    - Qdrant 服务正在运行（docker compose up -d qdrant）
    - ChromaDB 持久化目录存在（默认 ./chroma_db）
"""

import argparse
import sys

from core.logger import get_logger

logger = get_logger("migrate_chroma_to_qdrant")


def migrate_collection(chroma_client, qdrant_kb, collection_name: str) -> int:
    """迁移单个 collection 的数据到 Qdrant"""
    try:
        collection = chroma_client.get_collection(collection_name)
        count = collection.count()
        if count == 0:
            logger.info(f"Collection '{collection_name}' 为空，跳过")
            return 0

        all_data = collection.get(include=["documents", "metadatas"])
        documents = all_data.get("documents", [])
        metadatas = all_data.get("metadatas", [])

        if not documents:
            logger.info(f"Collection '{collection_name}' 无文档，跳过")
            return 0

        qdrant_kb.add_documents(collection_name, documents, metadatas)
        logger.info(f"Collection '{collection_name}' 迁移完成: {len(documents)} 条")
        return len(documents)
    except Exception as e:
        logger.error(f"Collection '{collection_name}' 迁移失败: {e}")
        return 0


def main():
    parser = argparse.ArgumentParser(description="ChromaDB → Qdrant 数据迁移")
    parser.add_argument(
        "--chroma-dir",
        default="./chroma_db",
        help="ChromaDB 持久化目录（默认 ./chroma_db）",
    )
    parser.add_argument("--qdrant-host", default="localhost", help="Qdrant 主机地址")
    parser.add_argument("--qdrant-port", type=int, default=6333, help="Qdrant REST API 端口")
    parser.add_argument("--qdrant-grpc-port", type=int, default=6334, help="Qdrant gRPC 端口")
    args = parser.parse_args()

    # 初始化 ChromaDB 客户端
    try:
        import chromadb

        chroma_client = chromadb.PersistentClient(path=args.chroma_dir)
        logger.info(f"ChromaDB 已连接（持久化目录: {args.chroma_dir}）")
    except Exception as e:
        logger.error(f"ChromaDB 连接失败: {e}")
        print("请确保 chromadb 已安装并且持久化目录存在")
        sys.exit(1)

    # 初始化 Qdrant 客户端
    try:
        from rag.qdrant_knowledge_base import QdrantKnowledgeBase

        qdrant_kb = QdrantKnowledgeBase(
            host=args.qdrant_host,
            port=args.qdrant_port,
            grpc_port=args.qdrant_grpc_port,
        )
        if not qdrant_kb.available:
            logger.error("Qdrant 不可用，请确保 Qdrant 服务正在运行")
            sys.exit(1)
        logger.info(f"Qdrant 已连接 ({args.qdrant_host}:{args.qdrant_port})")
    except Exception as e:
        logger.error(f"Qdrant 连接失败: {e}")
        print("请确保 qdrant-client 已安装并且 Qdrant 服务正在运行")
        sys.exit(1)

    # 执行迁移
    collections = ["product_knowledge", "faq", "tech_support", "complaint_knowledge"]
    total = 0
    for name in collections:
        total += migrate_collection(chroma_client, qdrant_kb, name)

    logger.info(f"迁移完成，共迁移 {total} 条文档")
    print(f"\n{'='*50}")
    print(f"  迁移完成: {total} 条文档从 ChromaDB 迁移到 Qdrant")
    print(f"  源: {args.chroma_dir}")
    print(f"  目标: {args.qdrant_host}:{args.qdrant_port}")
    print(f"{'='*50}")
    print("\n下一步: 在 .env 中设置 VECTOR_DB_MODE=qdrant_only 并重启应用")


if __name__ == "__main__":
    main()
