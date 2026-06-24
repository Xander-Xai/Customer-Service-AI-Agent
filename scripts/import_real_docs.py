#!/usr/bin/env python3
"""
真实业务文档导入脚本（从 CSV/JSON 导入到 Qdrant 知识库）。

用法:
    # 从 CSV 导入
    python3 scripts/import_real_docs.py --format csv --input docs.csv --scene 售前咨询

    # 从 JSON 导入
    python3 scripts/import_real_docs.py --format json --input products.json --scene 技术支持
"""

import argparse
import asyncio
import csv
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import QDRANT_HOST, QDRANT_PORT
from rag.qdrant_knowledge_base import QdrantKnowledgeBase

SCENE_CHOICES = ["售前咨询", "售后支持", "技术答疑", "投诉处理"]


async def import_from_csv(path: str, scene: str) -> int:
    """从 CSV 文件导入文档"""
    kb = QdrantKnowledgeBase(host=QDRANT_HOST, port=QDRANT_PORT)
    docs = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            doc = {
                "id": row.get("id", str(uuid.uuid4())),
                "title": row.get("title", ""),
                "content": row.get("content", ""),
                "category": row.get("category", "unknown"),
                "scene": scene,
                "tags": [t.strip() for t in row.get("tags", "").split(",") if t.strip()],
                "source": "import",
                "created_at": str(time.time()),
            }
            docs.append(doc)

    if docs:
        collection_name = f"scene_{scene}"
        kb.add_documents(
            collection_name=collection_name,
            documents=[d["content"] for d in docs],
            metadatas=[{
                "doc_id": d["id"],
                "title": d["title"],
                "category": d["category"],
                "scene": d["scene"],
                "tags": ",".join(d["tags"]),
                "source": d["source"],
                "created_at": d["created_at"],
            } for d in docs],
            ids=[d["id"] for d in docs],
        )
    return len(docs)


async def import_from_json(path: str, scene: str) -> int:
    """从 JSON 文件导入文档"""
    kb = QdrantKnowledgeBase(host=QDRANT_HOST, port=QDRANT_PORT)
    with open(path, encoding="utf-8") as f:
        raw_data = json.load(f)

    docs = raw_data if isinstance(raw_data, list) else raw_data.get("documents", [])
    if not docs:
        print("JSON 文件中未找到文档数据（期望数组或 {documents: [...]}）")
        return 0

    for doc in docs:
        doc.setdefault("id", str(uuid.uuid4()))
        doc.setdefault("scene", scene)
        doc.setdefault("source", "import")
        doc.setdefault("created_at", str(time.time()))
        doc.setdefault("tags", [])

    collection_name = f"scene_{scene}"
    kb.add_documents(
        collection_name=collection_name,
        documents=[d["content"] for d in docs],
        metadatas=[{
            "doc_id": d["id"],
            "title": d.get("title", ""),
            "category": d.get("category", "unknown"),
            "scene": d["scene"],
            "tags": ",".join(d.get("tags", [])),
            "source": d["source"],
            "created_at": d["created_at"],
        } for d in docs],
        ids=[d["id"] for d in docs],
    )
    return len(docs)


async def main():
    parser = argparse.ArgumentParser(description="导入真实业务文档到 Qdrant 知识库")
    parser.add_argument(
        "--format",
        choices=["csv", "json"],
        required=True,
        help="输入文件格式",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="输入文件路径",
    )
    parser.add_argument(
        "--scene",
        required=True,
        choices=SCENE_CHOICES,
        help="业务场景分类",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 文件不存在: {args.input}")
        sys.exit(1)

    print(f"正在导入: {args.input}")
    print(f"格式: {args.format}, 场景: {args.scene}")

    start = time.time()
    if args.format == "csv":
        count = await import_from_csv(args.input, args.scene)
    else:
        count = await import_from_json(args.input, args.scene)

    elapsed = time.time() - start
    print(f"成功导入 {count} 条文档到场景 '{args.scene}'（耗时 {elapsed:.2f}s）")


if __name__ == "__main__":
    asyncio.run(main())
