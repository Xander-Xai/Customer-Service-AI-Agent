"""
知识库管理路由（v4.0）
- GET    /api/knowledge/stats — 知识库统计
- POST   /api/knowledge/seed — 重新种子数据
- DELETE /api/knowledge/{collection}/{doc_id} — 删除文档
- POST   /api/knowledge/{collection}/add — 添加文档
- POST   /api/knowledge/sync — 从 ERP 同步产品数据
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth.router import require_admin
from logger import get_logger

logger = get_logger("knowledge.router")

router = APIRouter(prefix="/api/knowledge", tags=["知识库"])


class AddDocRequest(BaseModel):
    documents: list[str] = Field(..., min_length=1, max_length=50)
    metadatas: list[dict] | None = None


@router.get("/stats")
async def knowledge_stats(request: Request):
    """查看知识库统计（v4.0: 需要管理员权限）"""
    _ = require_admin(request)
    try:
        from multi_agent_customer_service import knowledge_base

        if not knowledge_base or not knowledge_base.available:
            return {"available": False, "message": "RAG 知识库未初始化"}
        collections = ["product_knowledge", "faq", "tech_support", "complaint_knowledge"]
        stats = {}
        for name in collections:
            stats[name] = knowledge_base.get_collection_count(name)
        return {"available": True, "collections": stats, "total": sum(stats.values())}
    except Exception as e:
        logger.error(f"知识库统计查询失败: {e}")
        return {"available": False, "message": "获取统计信息失败"}


@router.post("/seed")
async def reseed_knowledge(request: Request):
    """重新种子数据（admin only）"""
    _ = require_admin(request)
    try:
        from multi_agent_customer_service import knowledge_base

        if not knowledge_base or not knowledge_base.available:
            raise HTTPException(status_code=503, detail="RAG 知识库不可用")
        from rag.seed_data import (
            seed_complaint_knowledge,
            seed_faq,
            seed_product_knowledge,
            seed_supplementary_data,
            seed_tech_support,
        )

        seed_product_knowledge(knowledge_base)
        seed_faq(knowledge_base)
        seed_tech_support(knowledge_base)
        seed_complaint_knowledge(knowledge_base)
        seed_supplementary_data(knowledge_base)
        stats = {
            "product_knowledge": knowledge_base.get_collection_count("product_knowledge"),
            "faq": knowledge_base.get_collection_count("faq"),
            "tech_support": knowledge_base.get_collection_count("tech_support"),
            "complaint_knowledge": knowledge_base.get_collection_count("complaint_knowledge"),
        }
        return {"message": "种子数据已更新", "stats": stats}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"种子失败: {e}")


@router.post("/{collection}/add")
async def add_documents(collection: str, data: AddDocRequest, request: Request):
    """向知识库添加文档（admin only）"""
    _ = require_admin(request)
    try:
        from multi_agent_customer_service import knowledge_base

        if not knowledge_base or not knowledge_base.available:
            raise HTTPException(status_code=503, detail="RAG 知识库不可用")
        valid_collections = ["product_knowledge", "faq", "tech_support"]
        if collection not in valid_collections:
            raise HTTPException(
                status_code=400, detail=f"无效的 collection，可选: {valid_collections}"
            )
        knowledge_base.add_documents(
            collection,
            data.documents,
            data.metadatas,
        )
        count = knowledge_base.get_collection_count(collection)
        return {
            "message": f"已添加 {len(data.documents)} 条文档",
            "collection": collection,
            "total": count,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"添加失败: {e}")


@router.post("/sync")
async def sync_from_erp(request: Request):
    """从 ERP 同步产品数据到 RAG（admin only）
    v4.1: 支持分页查询 + 空结果处理
    """
    _ = require_admin(request)
    try:
        from multi_agent_customer_service import erp, knowledge_base

        if not knowledge_base or not knowledge_base.available:
            raise HTTPException(status_code=503, detail="RAG 知识库不可用")
        if not erp:
            raise HTTPException(status_code=503, detail="ERP 适配器未初始化")

        # 分页查询 ERP 产品（keyword="" 触发全量查询，适配器内部自动分页）
        all_products: list = []
        page = 0
        page_size = 50
        while True:
            try:
                # KingdeeRealAdapter 会自动处理分页，这里直接传空关键词
                # 如果适配器有 query_product_paged 方法则分页调用，否则一次性获取
                if page == 0:
                    products = await erp.query_product("")
                else:
                    break  # MockAdapter 不支持分页，一次返回全部
            except Exception as e:
                logger.warning(f"ERP 分页查询第 {page + 1} 页失败: {e}")
                break

            if not products:
                if page == 0:
                    return {"message": "ERP 未返回产品数据", "synced": 0}
                break

            all_products.extend(products)
            page += 1

            # 如果单次返回数量少于页大小，说明已经是最后一页
            if len(products) < page_size:
                break

        if not all_products:
            return {"message": "ERP 未返回产品数据", "synced": 0}

        documents = []
        metadatas = []
        for p in all_products:
            # 安全获取字段，缺失时用默认值
            name = p.get("name", "未知产品")
            category = p.get("category", "未分类")
            price = p.get("price", 0)
            specs = p.get("specs", "")
            ingredients = p.get("ingredients", "")
            suitable = p.get("suitable", "")

            doc = (
                f"产品: {name} | 类别: {category} | 价格: {price}元 "
                f"| 规格: {specs} | 成分: {ingredients} | 适用: {suitable}"
            )
            documents.append(doc)
            metadatas.append(
                {
                    "category": "product_type",
                    "topic": category,
                    "name": name,
                    "source": "erp_sync",
                }
            )

        if not documents:
            return {"message": "ERP 产品数据为空，无需同步", "synced": 0}

        collection_name = "product_knowledge"
        existing_count = knowledge_base.get_collection_count(collection_name)
        ids = [f"erp_{i:03d}" for i in range(existing_count, existing_count + len(documents))]
        knowledge_base.add_documents(collection_name, documents, metadatas, ids)
        total = knowledge_base.get_collection_count(collection_name)
        return {
            "message": f"从 ERP 同步 {len(documents)} 条产品数据",
            "synced": len(documents),
            "total": total,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"同步失败: {e}")
