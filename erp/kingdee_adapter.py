"""
金蝶 ERP Mock 适配器
模拟金蝶 API 调用，实际部署时替换为真实 HTTP 请求
"""

import asyncio
from typing import Any, Dict, List, Optional

from . import KingdeeAdapterBase


class KingdeeMockAdapter(KingdeeAdapterBase):
    """金蝶 ERP Mock 实现 - 化妆品生产企业数据"""

    def __init__(self):
        self._products = {
            "P001": {
                "id": "P001",
                "name": "玫瑰焕颜精华液",
                "category": "精华",
                "price": 298.0,
                "specs": "30ml",
                "ingredients": "玫瑰精油,透明质酸,烟酰胺",
                "suitable": "所有肤质",
            },
            "P002": {
                "id": "P002",
                "name": "绿茶控油洁面乳",
                "category": "洁面",
                "price": 128.0,
                "specs": "120ml",
                "ingredients": "绿茶提取物,水杨酸,甘油",
                "suitable": "油性/混合肤质",
            },
            "P003": {
                "id": "P003",
                "name": "玻尿酸保湿面膜",
                "category": "面膜",
                "price": 198.0,
                "specs": "5片/盒",
                "ingredients": "玻尿酸,胶原蛋白,维生素E",
                "suitable": "干性肤质",
            },
            "P004": {
                "id": "P004",
                "name": "烟酰胺美白霜",
                "category": "面霜",
                "price": 358.0,
                "specs": "50g",
                "ingredients": "烟酰胺,熊果苷,维C衍生物",
                "suitable": "暗沉肤质",
            },
            "P005": {
                "id": "P005",
                "name": "积雪草修护水",
                "category": "化妆水",
                "price": 168.0,
                "specs": "150ml",
                "ingredients": "积雪草提取物,神经酰胺,洋甘菊",
                "suitable": "敏感肤质",
            },
        }
        self._inventory = {
            "P001": {
                "product_name": "玫瑰焕颜精华液",
                "stock": 1200,
                "warehouse": "上海仓",
                "updated": "2026-05-30",
            },
            "P002": {
                "product_name": "绿茶控油洁面乳",
                "stock": 3500,
                "warehouse": "上海仓",
                "updated": "2026-05-30",
            },
            "P003": {
                "product_name": "玻尿酸保湿面膜",
                "stock": 800,
                "warehouse": "广州仓",
                "updated": "2026-05-29",
            },
            "P004": {
                "product_name": "烟酰胺美白霜",
                "stock": 450,
                "warehouse": "上海仓",
                "updated": "2026-05-30",
            },
            "P005": {
                "product_name": "积雪草修护水",
                "stock": 2100,
                "warehouse": "北京仓",
                "updated": "2026-05-28",
            },
        }
        self._orders = {
            "ORD20260530001": {
                "customer_id": "C001",
                "customer_name": "王女士",
                "items": ["玫瑰焕颜精华液 x2"],
                "total": 596.0,
                "status": "已发货",
                "tracking": "SF1234567890",
                "created": "2026-05-28",
            },
            "ORD20260530002": {
                "customer_id": "C002",
                "customer_name": "李女士",
                "items": ["烟酰胺美白霜 x1", "绿茶控油洁面乳 x1"],
                "total": 486.0,
                "status": "待发货",
                "tracking": "",
                "created": "2026-05-30",
            },
            "ORD20260530003": {
                "customer_id": "C001",
                "customer_name": "王女士",
                "items": ["积雪草修护水 x3"],
                "total": 504.0,
                "status": "已完成",
                "tracking": "SF9876543210",
                "created": "2026-05-25",
            },
        }
        self._customers = {
            "C001": {
                "id": "C001",
                "name": "王女士",
                "phone": "138****1234",
                "level": "VIP",
                "total_orders": 12,
                "total_spent": 5680.0,
                "address": "上海市浦东新区",
            },
            "C002": {
                "id": "C002",
                "name": "李女士",
                "phone": "139****5678",
                "level": "普通会员",
                "total_orders": 3,
                "total_spent": 1260.0,
                "address": "北京市朝阳区",
            },
        }

    async def query_product(self, keyword: str) -> list[dict[str, Any]]:
        await asyncio.sleep(0.1)
        keyword = keyword.lower()
        return [
            p
            for p in self._products.values()
            if keyword in p["name"].lower()
            or keyword in p["category"].lower()
            or keyword in p["ingredients"].lower()
        ]

    async def query_inventory(
        self, product_id: str = "", keyword: str = ""
    ) -> list[dict[str, Any]]:
        await asyncio.sleep(0.1)
        if product_id:
            inv = self._inventory.get(product_id)
            return [{**inv, "product_id": product_id}] if inv else []
        if keyword:
            return [
                {**v, "product_id": k}
                for k, v in self._inventory.items()
                if keyword.lower() in v["product_name"].lower()
            ]
        return [{**v, "product_id": k} for k, v in self._inventory.items()]

    async def query_order(self, order_id: str = "", customer_id: str = "") -> list[dict[str, Any]]:
        await asyncio.sleep(0.1)
        if order_id:
            o = self._orders.get(order_id)
            return [{**o, "order_id": order_id}] if o else []
        if customer_id:
            return [
                {**v, "order_id": k}
                for k, v in self._orders.items()
                if v["customer_id"] == customer_id
            ]
        return [{**v, "order_id": k} for k, v in self._orders.items()]

    async def query_customer(self, customer_id: str) -> dict[str, Any] | None:
        await asyncio.sleep(0.1)
        return self._customers.get(customer_id)
