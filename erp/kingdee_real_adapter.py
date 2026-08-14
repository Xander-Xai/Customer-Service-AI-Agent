"""
金蝶 ERP 真实适配器（v4.1 生产加固版）
对接金蝶 Cloud API（REST），实现商品/库存/订单/客户查询

v3.4: 所有 FilterString 参数经过 sanitize_erp_input 净化，防 SQL 注入
v4.1: 指数退避重试 + Token 自动刷新 + 分页查询 + 字段标准化映射
"""

import asyncio
import functools
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from core.config import (
    ERP_USER_CUSTOMER_MAP,
    HTTP_TIMEOUT,
    HTTPX_KEEPALIVE_CONNECTIONS,
    HTTPX_MAX_CONNECTIONS,
    RETRY_BASE_DELAY,
    RETRY_MAX_ATTEMPTS,
)
from core.logger import get_logger
from erp import KingdeeAdapterBase, sanitize_erp_input
from erp.authorization import sanitize_resource_id

logger = get_logger("erp.kingdee_real")

# ---------------------------------------------------------------------------
# 指数退避重试装饰器
# ---------------------------------------------------------------------------


def _exponential_backoff(
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
    exceptions: tuple = (Exception,),
):
    """指数退避重试装饰器，支持可配置的重试次数和基础延迟"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts - 1:
                        delay = base_delay * (2**attempt)
                        logger.warning(
                            f"{func.__name__} 第 {attempt + 1} 次失败: {exc}, {delay:.1f}s 后重试"
                        )
                        await asyncio.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# KingdeeRealAdapter
# ---------------------------------------------------------------------------


class KingdeeRealAdapter(KingdeeAdapterBase):
    """
    金蝶 Cloud API 真实适配器
    认证方式: AppID + AppSecret HMAC 签名

    v4.1 改进:
    - Token 缓存 + 过期前 5 分钟自动刷新
    - 指数退避重试（最多 3 次）
    - HTTP 请求超时控制（默认 30s）
    - 分页查询自动合并
    - 字段名标准化映射（金蝶字段 -> MockAdapter 标准字段）
    """

    # Token 提前刷新的秒数（过期前 5 分钟）
    _TOKEN_REFRESH_BUFFER = 300

    # 金蝶 FilterString 安全字符白名单（仅允许字母、数字、中文、常见标点）
    _SAFE_FILTER_RE = re.compile(r"[^a-zA-Z0-9一-鿿._\s]")

    # 分页：每次请求的最大行数
    _PAGE_SIZE = 100

    @classmethod
    def _sanitize_filter_value(cls, value: str) -> str:
        """清理 FilterString 中的用户输入，防止注入攻击"""
        return cls._SAFE_FILTER_RE.sub("", value)

    def __init__(
        self,
        base_url: str,
        app_id: str,
        app_secret: str,
        db_id: str,
        user_customer_map: dict[str, str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self.db_id = db_id
        # P0-03 Remaining Risk #4: 权威 user_id → customer_id 映射。默认取自
        # 可验证的服务端配置 ERP_USER_CUSTOMER_MAP（环境变量 JSON）；测试或
        # 特殊部署可显式注入。缺失/未配置时为空 dict -> resolve fail closed。
        self._user_customer_map = (
            user_customer_map if user_customer_map is not None else ERP_USER_CUSTOMER_MAP
        )
        self._token: str | None = None
        self._token_obtained_at: float = 0
        self._token_expires: float = 0
        self._token_refresh_lock: asyncio.Lock | None = None
        self._client: httpx.AsyncClient | None = None

    def _get_refresh_lock(self) -> asyncio.Lock:
        """延迟创建锁（避免在非异步上下文中创建）"""
        if self._token_refresh_lock is None:
            self._token_refresh_lock = asyncio.Lock()
        return self._token_refresh_lock

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(HTTP_TIMEOUT),
                limits=httpx.Limits(
                    max_connections=HTTPX_MAX_CONNECTIONS,
                    max_keepalive_connections=HTTPX_KEEPALIVE_CONNECTIONS,
                ),
                trust_env=False,
            )
        return self._client

    # -------------------------------------------------------------------
    # Token 管理
    # -------------------------------------------------------------------

    def _token_is_valid(self) -> bool:
        """判断 token 是否仍在有效期内（含提前刷新缓冲）"""
        if not self._token:
            return False
        return time.time() < (self._token_expires - self._TOKEN_REFRESH_BUFFER)

    async def _refresh_token(self) -> None:
        """实际执行 token 获取/刷新的底层方法（不加锁）"""
        client = await self._get_client()
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
        self._token_obtained_at = time.time()
        # 金蝶 token 有效期约 2 小时（7200s），我们记录绝对过期时间
        self._token_expires = time.time() + 7200
        logger.info("金蝶 token 获取成功")

    @_exponential_backoff(
        max_attempts=3,
        base_delay=1.0,
        exceptions=(Exception,),
    )
    async def _ensure_token(self) -> None:
        """获取/刷新 access_token（线程安全 + 并发去重）"""
        if self._token_is_valid():
            return

        lock = self._get_refresh_lock()
        async with lock:
            # double-check：拿到锁后再检查一次
            if self._token_is_valid():
                return
            await self._refresh_token()

    # -------------------------------------------------------------------
    # 通用 API 调用（带重试 + Token 刷新）
    # -------------------------------------------------------------------

    @_exponential_backoff(
        max_attempts=3,
        base_delay=1.0,
        exceptions=(httpx.HTTPStatusError, httpx.RequestError),
    )
    async def _api_call(self, form_id: str, method: str, data: dict[str, Any]) -> Any:
        """
        通用金蝶 API 调用
        - 401/403 时自动刷新 token 并重试
        - 其他 HTTP 错误进入退避重试
        """
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
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (401, 403):
                # Token 可能已过期，强制刷新后重试
                logger.warning(f"金蝶 API 返回 {status}，强制刷新 token")
                self._token = None
                self._token_expires = 0
                await self._ensure_token()
                # 重试一次
                resp2 = await client.post(
                    f"{self.base_url}/k3cloud/{form_id}/{method}",
                    json=data,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
                resp2.raise_for_status()
                return resp2.json()
            logger.error(f"金蝶 API 调用失败 ({form_id}/{method}): {e}", exc_info=True)
            raise
        except httpx.RequestError as e:
            # 网络异常（连接超时、DNS 解析失败等）
            logger.error(f"金蝶 API 网络异常 ({form_id}/{method}): {e}", exc_info=True)
            raise

    # -------------------------------------------------------------------
    # 分页查询辅助
    # -------------------------------------------------------------------

    async def _paged_query(
        self,
        form_id: str,
        filter_string: str,
        field_names: list[str] | None = None,
        top_row_count: int = 0,
    ) -> list[dict[str, Any]]:
        """
        带分页的金蝶 BillQuery 封装。
        - top_row_count=0 表示查询全部（自动分页）
        - 返回合并后的所有行
        """
        all_rows: list[dict[str, Any]] = []
        start_row = 0

        while True:
            body: dict[str, Any] = {
                "FormId": form_id,
                "FilterString": filter_string,
                "StartRow": start_row,
                "Limit": self._PAGE_SIZE,
            }
            if field_names:
                body["FieldNames"] = field_names
            if top_row_count > 0:
                remaining = top_row_count - len(all_rows)
                if remaining <= 0:
                    break
                body["Limit"] = min(self._PAGE_SIZE, remaining)

            result = await self._api_call(form_id, "BillQuery", body)
            rows = result.get("Data", {})
            if isinstance(rows, dict):
                rows = rows.get("Rows", [])
            if not rows:
                break

            all_rows.extend(rows)
            start_row += len(rows)

            # 如果返回行数不足一页，说明已经是最后一页
            if len(rows) < self._PAGE_SIZE:
                break
            # 如果已达到请求上限
            if top_row_count > 0 and len(all_rows) >= top_row_count:
                break

        # 确保不超过请求上限（防御性截断）
        if top_row_count > 0 and len(all_rows) > top_row_count:
            all_rows = all_rows[:top_row_count]

        return all_rows

    # -------------------------------------------------------------------
    # 字段映射：金蝶原始字段 -> 标准字段（与 MockAdapter 一致）
    # -------------------------------------------------------------------

    @staticmethod
    def _map_product(row: dict[str, Any]) -> dict[str, Any]:
        """金蝶物料行 -> 标准产品字段"""
        return {
            "id": row.get("FNumber", ""),
            "name": row.get("FName", ""),
            "category": row.get("FCategory", ""),
            "price": float(row.get("FPrice", 0) or 0),
            "specs": row.get("FSpecification", ""),
            "ingredients": row.get("FDescription", ""),
            "suitable": row.get("FAuxProperty", ""),
        }

    @staticmethod
    def _map_inventory(row: dict[str, Any]) -> dict[str, Any]:
        """金蝶库存行 -> 标准库存字段"""
        material = row.get("FMaterialId", {})
        if isinstance(material, str):
            material = {}
        warehouse = row.get("FStockId", {})
        if isinstance(warehouse, str):
            warehouse = {}
        return {
            "product_id": material.get("FNumber", ""),
            "product_name": material.get("FName", ""),
            "stock": int(row.get("FQty", 0) or 0),
            "warehouse": warehouse.get("FName", ""),
            "updated": row.get("FDate", ""),
        }

    @staticmethod
    def _map_order(row: dict[str, Any]) -> dict[str, Any]:
        """金蝶销售订单行 -> 标准订单字段"""
        customer = row.get("FCUSTID", {})
        if isinstance(customer, str):
            customer = {}
        bill_entries = row.get("BillEntry", [])
        if isinstance(bill_entries, str):
            bill_entries = []
        items: list[str] = []
        for entry in bill_entries:
            if isinstance(entry, dict):
                mat = entry.get("FMaterialId", {})
                if isinstance(mat, dict):
                    items.append(mat.get("FName", ""))
                elif isinstance(mat, str):
                    items.append(mat)
        return {
            "order_id": row.get("FBillNo", ""),
            "customer_id": customer.get("FNumber", ""),
            "customer_name": customer.get("FName", ""),
            "total": float(row.get("FOrderAmount", 0) or 0),
            "status": row.get("FStatus", ""),
            "tracking": row.get("FTrackingNo", ""),
            "created": row.get("FDate", ""),
            "items": items,
        }

    @staticmethod
    def _map_customer(row: dict[str, Any]) -> dict[str, Any]:
        """金蝶客户行 -> 标准客户字段"""
        return {
            "id": row.get("FNumber", ""),
            "name": row.get("FName", ""),
            "phone": row.get("FPhoneNumber", ""),
            "level": row.get("FVIPLevel", "普通"),
            "address": row.get("FAddress", ""),
        }

    # -------------------------------------------------------------------
    # 业务查询方法
    # -------------------------------------------------------------------

    async def query_product(self, keyword: str) -> list[dict[str, Any]]:
        """查询商品信息（映射到金蝶物料表 BD_MATERIAL）"""
        try:
            safe_keyword = sanitize_erp_input(keyword)
            filter_str = f"FName like '%{safe_keyword}%'" if safe_keyword else ""
            rows = await self._paged_query("BD_MATERIAL", filter_str)
            return [self._map_product(row) for row in rows]
        except Exception as e:
            logger.warning(f"产品查询失败: {e}")
            return []

    async def query_inventory(
        self, product_id: str = "", keyword: str = ""
    ) -> list[dict[str, Any]]:
        """查询库存（映射到金蝶库存查询 STK_INVENTORY）"""
        try:
            safe_pid = sanitize_erp_input(product_id)
            safe_kw = sanitize_erp_input(keyword)
            if product_id:
                filter_str = f"FMaterialId.FNumber='{safe_pid}'"
            elif keyword:
                filter_str = f"FMaterialId.FName like '%{safe_kw}%'"
            else:
                filter_str = ""
            rows = await self._paged_query("STK_INVENTORY", filter_str)
            return [self._map_inventory(row) for row in rows]
        except Exception as e:
            logger.warning(f"库存查询失败: {e}")
            return []

    async def query_order(self, order_id: str = "", customer_id: str = "") -> list[dict[str, Any]]:
        """查询订单（映射到金蝶销售订单 SAL_ORDER）"""
        if not order_id and not customer_id:
            logger.warning(
                "query_order 被调用时未提供 order_id 或 customer_id，拒绝查询以防止全量数据泄漏"
            )
            return []
        try:
            if order_id:
                safe_oid = sanitize_erp_input(order_id)
                filter_str = f"FBillNo='{safe_oid}'"
            else:
                safe_cid = sanitize_erp_input(customer_id)
                filter_str = f"FCUSTID.FNumber='{safe_cid}'"
            rows = await self._paged_query("SAL_ORDER", filter_str)
            return [self._map_order(row) for row in rows]
        except Exception as e:
            logger.warning(f"订单查询失败: {e}")
            return []

    async def query_customer(self, customer_id: str) -> dict[str, Any] | None:
        """查询客户资料（映射到金蝶客户表 BD_CUSTOMER）"""
        if not customer_id:
            logger.warning("query_customer 被调用时未提供 customer_id，拒绝查询")
            return None
        try:
            safe_cid = sanitize_erp_input(customer_id)
            rows = await self._paged_query("BD_CUSTOMER", f"FNumber='{safe_cid}'")
            if not rows:
                return None
            return self._map_customer(rows[0])
        except Exception as e:
            logger.warning(f"客户查询失败: {e}")
            return None

    async def resolve_customer_by_user(self, user_id: str | None) -> str | None:
        """P0-03 Remaining Risk #4: 权威 user_id → customer_id 解析。

        映射取自服务端可验证配置 ERP_USER_CUSTOMER_MAP（环境变量 JSON），
        绝不来自 prompt / LLM / 资源自声明。未配置 / 用户不在映射中 /
        身份缺失时返回 None（fail closed）-> ErpAuthorizationService 拒绝
        任何私人 ERP 资源。
        """
        if not user_id:
            return None
        return self._user_customer_map.get(user_id)

    async def get_order_owner(self, order_id: str) -> str | None:
        """P0-03: 最小归属元数据查询 — 仅向金蝶请求订单归属 customer_id
        （FCUSTID.FNumber），不取完整订单正文。

        供 ErpAuthorizationService 在披露完整 payload 前做 ownership 判定
        （AUTHZ-6）：拒绝时完整订单 payload 不进入 authz / Agent / LLM 内存。
        订单不存在、查询失败或字段缺失时返回 None（fail closed）。
        """
        if not order_id:
            return None
        try:
            safe_oid = sanitize_erp_input(order_id)
            rows = await self._paged_query(
                "SAL_ORDER",
                f"FBillNo='{safe_oid}'",
                field_names=["FCUSTID.FNumber"],
                top_row_count=1,
            )
            if not rows:
                return None
            first = rows[0]
            # Kingdee 通常以嵌套对象返回 FCUSTID；兼容扁平字段名。
            cust = first.get("FCUSTID", first.get("FCUSTID_FNumber", ""))
            if isinstance(cust, dict):
                return cust.get("FNumber", "") or None
            return cust or None
        except Exception as e:
            # AC16: sanitize the resource id before logging — the Aftersales
            # path passes a natural-language query as order_id, which may
            # carry PII (e.g. a phone number, or "tel"+digits). Reuse the
            # shared AuthZ policy: only a provably-valid ERP ORDER id
            # (^ORD\d+$, ≤64 chars) is logged verbatim; everything else is
            # hashed, so no raw prompt/PII enters any erp.* log.
            safe_oid = sanitize_resource_id(order_id, "order")
            logger.warning(f"get_order_owner 失败 (order_id={safe_oid}): {e}")
            return None

    # -------------------------------------------------------------------
    # 生命周期
    # -------------------------------------------------------------------

    async def close(self):
        """关闭 HTTP 客户端和清理资源"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._token = None
        self._token_expires = 0
