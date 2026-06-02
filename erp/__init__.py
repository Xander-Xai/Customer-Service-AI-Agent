"""金蝶 ERP 模块"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class KingdeeAdapterBase(ABC):
    """金蝶 ERP 适配器基类"""

    @abstractmethod
    async def query_product(self, keyword: str) -> List[Dict[str, Any]]:
        """查询商品信息"""

    @abstractmethod
    async def query_inventory(self, product_id: str = "", keyword: str = "") -> List[Dict[str, Any]]:
        """查询库存余量"""

    @abstractmethod
    async def query_order(self, order_id: str = "", customer_id: str = "") -> List[Dict[str, Any]]:
        """查询订单状态"""

    @abstractmethod
    async def query_customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        """查询客户资料"""
