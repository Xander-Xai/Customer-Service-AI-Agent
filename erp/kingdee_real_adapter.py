"""
金蝶 ERP 真实适配器（v3.4 安全加固版）
对接金蝶 Cloud API（REST），实现商品/库存/订单/客户查询
v3.4: 所有 FilterString 参数经过 sanitize_erp_input 净化，防 SQL 注入
"""
import re
import time
from typing import Any, Dict, List, Optional

import httpx
from erp import KingdeeAdapterBase, sanitize_erp_input
from config import HTTP_TIMEOUT, HTTPX_MAX_CONNECTIONS, HTTPX_KEEPALIVE_CONNECTIONS
from logger import get_logger

logger = get_logger("erp.kingdee_real")


class KingdeeRealAdapter(KingdeeAdapterBase):
    """
    金蝶 Cloud API 真实适配器
    认证方式: AppID + AppSecret HMAC 签名
    """

    # 金蝶 FilterString 安全字符白名单（仅允许字母、数字、中文、常见标点）
    _SAFE_FILTER_RE = re.compile(r"[^a-zA-Z0-9一-鿿._\s]")

    @classmethod
    def _sanitize_filter_value(cls, value: str) -> str:
        """清理 FilterString 中的用户输入，防止注入攻击"""
        return cls._SAFE_FILTER_RE.sub("", value)

    def __init__(self, base_url: str, app_id: str, app_secret: str, db_id: str):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self.db_id = db_id
        self._token: Optional[str] = None
        self._token_expires: float = 0
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(HTTP_TIMEOUT),
                limits=httpx.Limits(max_connections=HTTPX_MAX_CONNECTIONS, max_keepalive_connections=HTTPX_KEEPALIVE_CONNECTIONS),
            )
        return self._client

    async def _ensure_token(self):
        """获取/刷新 access_token"""
        if self._token and time.time() < self._token_expires:
            return
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{self.base_url}/k3cloud/Logon.aspx",
                json={
                    "acctID": self.db_id,
                    "username": self.app_id,
                    "password": self.app_secret,
                    "lcid": 2052,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data.get("Token", "")
            self._token_expires = time.time() + 7000  # 金蝶 token 有效期约 2 小时
            logger.info("金蝶 token 获取成功")
        except Exception as e:
            logger.error(f"金蝶 token 获取失败: {e}")
            raise

    async def _api_call(self, form_id: str, method: str, data: Dict[str, Any]) -> Any:
        """通用金蝶 API 调用"""
        await self._ensure_token()
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{self.base_url}/k3cloud/{form_id}/{method}",
                json=data,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"金蝶 API 调用失败 ({form_id}/{method}): {e}")
            raise

    async def query_product(self, keyword: str) -> List[Dict[str, Any]]:
        """查询商品信息（映射到金蝶物料表 BD_MATERIAL）"""
        safe_keyword = self._sanitize_filter_value(keyword)
        try:
            safe_keyword = sanitize_erp_input(keyword)  # v3.4: 防注入
            result = await self._api_call(
                "BD_MATERIAL", "BillQuery",
                {"FormId": "BD_MATERIAL", "FilterString": f"FName like '%{safe_keyword}%'", "TopRowCount": 10}
            )
            items = result.get("Data", {}).get("Rows", [])
            return [
                {
                    "id": row.get("FNumber", ""),
                    "name": row.get("FName", ""),
                    "category": row.get("FCategory", ""),
                    "price": float(row.get("FPrice", 0)),
                    "specs": row.get("FSpecification", ""),
                    "ingredients": row.get("FDescription", ""),
                    "suitable": row.get("FAuxProperty", ""),
                }
                for row in items
            ]
        except Exception as e:
            logger.warning(f"产品查询失败: {e}")
            return []

    async def query_inventory(self, product_id: str = "", keyword: str = "") -> List[Dict[str, Any]]:
        """查询库存（映射到金蝶库存查询 STK_INVENTORY）"""
        safe_product_id = self._sanitize_filter_value(product_id)
        safe_keyword = self._sanitize_filter_value(keyword)
        try:
            safe_pid = sanitize_erp_input(product_id)  # v3.4: 防注入（白名单+转义）
            safe_kw = sanitize_erp_input(keyword)      # v3.4: 防注入（白名单+转义）
            filter_str = f"FMaterialId.FNumber='{safe_pid}'" if product_id else f"FMaterialId.FName like '%{safe_kw}%'"
            result = await self._api_call(
                "STK_INVENTORY", "BillQuery",
                {"FormId": "STK_INVENTORY", "FilterString": filter_str, "TopRowCount": 10}
            )
            items = result.get("Data", {}).get("Rows", [])
            return [
                {
                    "product_id": row.get("FMaterialId", {}).get("FNumber", ""),
                    "product_name": row.get("FMaterialId", {}).get("FName", ""),
                    "stock": int(row.get("FQty", 0)),
                    "warehouse": row.get("FStockId", {}).get("FName", ""),
                    "updated": row.get("FDate", ""),
                }
                for row in items
            ]
        except Exception as e:
            logger.warning(f"库存查询失败: {e}")
            return []

    async def query_order(self, order_id: str = "", customer_id: str = "") -> List[Dict[str, Any]]:
        """查询订单（映射到金蝶销售订单 SAL_ORDER）"""
        safe_order_id = self._sanitize_filter_value(order_id)
        safe_customer_id = self._sanitize_filter_value(customer_id)
        try:
            filter_str = ""
            if order_id:
                safe_oid = sanitize_erp_input(order_id)  # v3.4: 防注入（白名单+转义）
                filter_str = f"FBillNo='{safe_oid}'"
            elif customer_id:
                safe_cid = sanitize_erp_input(customer_id)  # v3.4: 防注入（白名单+转义）
                filter_str = f"FCUSTID.FNumber='{safe_cid}'"
            result = await self._api_call(
                "SAL_ORDER", "BillQuery",
                {"FormId": "SAL_ORDER", "FilterString": filter_str, "TopRowCount": 10}
            )
            items = result.get("Data", {}).get("Rows", [])
            return [
                {
                    "order_id": row.get("FBillNo", ""),
                    "customer_id": row.get("FCUSTID", {}).get("FNumber", ""),
                    "customer_name": row.get("FCUSTID", {}).get("FName", ""),
                    "total": float(row.get("FOrderAmount", 0)),
                    "status": row.get("FStatus", ""),
                    "tracking": row.get("FTrackingNo", ""),
                    "created": row.get("FDate", ""),
                    "items": [entry.get("FMaterialId", {}).get("FName", "") for entry in row.get("BillEntry", [])],
                }
                for row in items
            ]
        except Exception as e:
            logger.warning(f"订单查询失败: {e}")
            return []

    async def query_customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        """查询客户资料（映射到金蝶客户表 BD_CUSTOMER）"""
        safe_customer_id = self._sanitize_filter_value(customer_id)
        try:
            safe_cid = sanitize_erp_input(customer_id)  # v3.4: 防注入（白名单+转义）
            result = await self._api_call(
                "BD_CUSTOMER", "BillQuery",
                {"FormId": "BD_CUSTOMER", "FilterString": f"FNumber='{safe_cid}'"}
            )
            items = result.get("Data", {}).get("Rows", [])
            if not items:
                return None
            row = items[0]
            return {
                "id": row.get("FNumber", ""),
                "name": row.get("FName", ""),
                "phone": row.get("FPhoneNumber", ""),
                "level": row.get("FVIPLevel", "普通"),
                "address": row.get("FAddress", ""),
            }
        except Exception as e:
            logger.warning(f"客户查询失败: {e}")
            return None

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
