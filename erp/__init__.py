"""金蝶 ERP 模块（v3.4: 输入净化防 SQL 注入）"""

import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


def sanitize_erp_input(value: str, max_length: int = 100) -> str:
    """v3.4: 净化 ERP 查询输入，防止 SQL 注入 / FilterString 注入
    白名单策略：仅允许字母数字、中文、空格和少量安全标点。
    单引号转义为双单引号（SQL 标准）。
    """
    if not value:
        return ""
    # 白名单：仅保留安全字符
    cleaned = re.sub(r"[^\w\s一-鿿.,+]", "", value)
    # SQL 标准转义单引号
    cleaned = cleaned.replace("'", "''")
    # 移除多余空白
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_length]


class KingdeeAdapterBase(ABC):
    """金蝶 ERP 适配器基类"""

    @abstractmethod
    async def query_product(self, keyword: str) -> list[dict[str, Any]]:
        """查询商品信息"""

    @abstractmethod
    async def query_inventory(
        self, product_id: str = "", keyword: str = ""
    ) -> list[dict[str, Any]]:
        """查询库存余量"""

    @abstractmethod
    async def query_order(self, order_id: str = "", customer_id: str = "") -> list[dict[str, Any]]:
        """查询订单状态"""

    @abstractmethod
    async def query_customer(self, customer_id: str) -> dict[str, Any] | None:
        """查询客户资料"""
