"""
ERP 工具注册（v3.5）
将 ERP 适配器的 4 个查询方法包装为 OpenAI Function Calling 工具。
"""

from typing import Any

from core.logger import get_logger
from core.tool_result_cache import ToolCachePolicy

from .tool_registry import ToolRegistry

logger = get_logger("tools.erp")


def create_erp_tools(erp_adapter) -> ToolRegistry:
    """
    将 ERP 适配器方法注册为工具集。
    返回一个配置好的 ToolRegistry 实例。
    """
    registry = ToolRegistry()

    # ---- query_product ----
    async def _query_product(args: dict[str, Any]) -> Any:
        keyword = args.get("keyword", "")
        results = await erp_adapter.query_product(keyword)
        if not results:
            return f"未找到与 '{keyword}' 相关的产品"
        return results

    registry.register(
        name="query_product",
        description="查询化妆品产品信息，包括名称、类别、价格、规格、成分、适用肤质",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，可以是产品名称、类别或成分",
                }
            },
            "required": ["keyword"],
        },
        handler=_query_product,
        cache_policy=ToolCachePolicy(enabled=True, ttl_seconds=300),
    )

    # ---- query_inventory ----
    async def _query_inventory(args: dict[str, Any]) -> Any:
        product_id = args.get("product_id", "")
        keyword = args.get("keyword", "")
        results = await erp_adapter.query_inventory(product_id=product_id, keyword=keyword)
        if not results:
            return "未找到库存信息"
        return results

    registry.register(
        name="query_inventory",
        description="查询产品库存信息，包括库存数量、仓库位置",
        parameters={
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "string",
                    "description": "产品编号（如 P001）",
                },
                "keyword": {
                    "type": "string",
                    "description": "产品名称关键词",
                },
            },
        },
        handler=_query_inventory,
        cache_policy=ToolCachePolicy(enabled=True, ttl_seconds=30),
    )

    # ---- query_order ----
    async def _query_order(args: dict[str, Any]) -> Any:
        order_id = args.get("order_id", "")
        customer_id = args.get("customer_id", "")
        results = await erp_adapter.query_order(order_id=order_id, customer_id=customer_id)
        if not results:
            return "未找到订单信息"
        return results[:5]

    registry.register(
        name="query_order",
        description="查询订单状态、金额、物流信息",
        parameters={
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "订单编号（如 ORD20260530001）",
                },
                "customer_id": {
                    "type": "string",
                    "description": "客户编号（如 C001）",
                },
            },
        },
        handler=_query_order,
        # Private resource: ownership is resolved per request inside the
        # handler; never reuse a cached result across an authorization change.
        cache_policy=ToolCachePolicy(
            enabled=True, ttl_seconds=30, authorization_required=True
        ),
    )

    # ---- query_customer ----
    async def _query_customer(args: dict[str, Any]) -> Any:
        customer_id = args.get("customer_id", "")
        result = await erp_adapter.query_customer(customer_id)
        if not result:
            return f"未找到客户 '{customer_id}' 的信息"
        return {"customer_id": customer_id, **result}

    registry.register(
        name="query_customer",
        description="查询客户资料，包括姓名、电话、会员等级、消费记录、地址",
        parameters={
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "客户编号（如 C001）",
                }
            },
            "required": ["customer_id"],
        },
        handler=_query_customer,
        # Private resource: same re-authorization requirement as query_order.
        cache_policy=ToolCachePolicy(
            enabled=True, ttl_seconds=30, authorization_required=True
        ),
    )

    logger.info(f"ERP 工具注册完成: {registry.list_tools()}")
    return registry
