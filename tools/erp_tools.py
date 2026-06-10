"""
ERP 工具注册（v3.5）
将 ERP 适配器的 4 个查询方法包装为 OpenAI Function Calling 工具。
"""

from typing import Any

from core.logger import get_logger

from .tool_registry import ToolRegistry

logger = get_logger("tools.erp")


def create_erp_tools(erp_adapter) -> ToolRegistry:
    """
    将 ERP 适配器方法注册为工具集。
    返回一个配置好的 ToolRegistry 实例。
    """
    registry = ToolRegistry()

    # ---- query_product ----
    async def _query_product(args: dict[str, Any]) -> str:
        keyword = args.get("keyword", "")
        results = await erp_adapter.query_product(keyword)
        if not results:
            return f"未找到与 '{keyword}' 相关的产品"
        lines = []
        for p in results:
            lines.append(
                f"产品: {p['name']} | 类别: {p['category']} | 价格: {p['price']}元 "
                f"| 规格: {p['specs']} | 成分: {p['ingredients']} | 适用: {p['suitable']}"
            )
        return "\n".join(lines)

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
    )

    # ---- query_inventory ----
    async def _query_inventory(args: dict[str, Any]) -> str:
        product_id = args.get("product_id", "")
        keyword = args.get("keyword", "")
        results = await erp_adapter.query_inventory(product_id=product_id, keyword=keyword)
        if not results:
            return "未找到库存信息"
        lines = []
        for inv in results:
            lines.append(
                f"产品: {inv['product_name']} | 余量: {inv['stock']}件 "
                f"| 仓库: {inv['warehouse']} | 更新: {inv.get('updated', 'N/A')}"
            )
        return "\n".join(lines)

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
    )

    # ---- query_order ----
    async def _query_order(args: dict[str, Any]) -> str:
        order_id = args.get("order_id", "")
        customer_id = args.get("customer_id", "")
        results = await erp_adapter.query_order(order_id=order_id, customer_id=customer_id)
        if not results:
            return "未找到订单信息"
        lines = []
        for o in results[:5]:
            lines.append(
                f"订单: {o.get('order_id', '')} | 客户: {o.get('customer_name', '')} "
                f"| 状态: {o['status']} | 金额: {o['total']}元 "
                f"| 物流: {o.get('tracking', '无')} | 日期: {o.get('created', '')}"
            )
        return "\n".join(lines)

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
    )

    # ---- query_customer ----
    async def _query_customer(args: dict[str, Any]) -> str:
        customer_id = args.get("customer_id", "")
        result = await erp_adapter.query_customer(customer_id)
        if not result:
            return f"未找到客户 '{customer_id}' 的信息"
        return (
            f"客户: {result.get('name', '')} | 电话: {result.get('phone', '')} "
            f"| 等级: {result.get('level', '')} | 累计消费: {result.get('total_spent', 0)}元 "
            f"| 订单数: {result.get('total_orders', 0)} | 地址: {result.get('address', '')}"
        )

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
    )

    logger.info(f"ERP 工具注册完成: {registry.list_tools()}")
    return registry
