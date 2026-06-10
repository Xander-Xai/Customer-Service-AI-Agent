"""
ERP 集成测试（v4.1）
测试 KingdeeRealAdapter 的数据格式兼容性、重试逻辑、Token 过期处理
所有测试使用 Mock，不需要真实 API
"""

import os
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from erp.kingdee_adapter import KingdeeMockAdapter  # noqa: E402
from erp.kingdee_real_adapter import KingdeeRealAdapter, _exponential_backoff  # noqa: E402

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_adapter() -> KingdeeMockAdapter:
    return KingdeeMockAdapter()


@pytest.fixture
def real_adapter() -> KingdeeRealAdapter:
    return KingdeeRealAdapter(
        base_url="https://mock-kingdee.example.com",
        app_id="test_app",
        app_secret="test_secret",
        db_id="test_db",
    )


def _make_mock_client(post_side_effect=None, post_return=None) -> AsyncMock:
    """构造 httpx.AsyncClient 模拟对象，is_closed=False 防止 _get_client 创建真实客户端"""
    client = AsyncMock()
    client.is_closed = False
    if post_side_effect is not None:
        client.post = AsyncMock(side_effect=post_side_effect)
    elif post_return is not None:
        client.post = AsyncMock(return_value=post_return)
    else:
        client.post = AsyncMock()
    return client


def _make_http_response(status_code: int = 200, json_data: Any = None) -> httpx.Response:
    """构造 httpx.Response 模拟对象"""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"{status_code}",
            request=MagicMock(),
            response=resp,
        )
    return resp


# ============================================================================
# 1. 数据格式兼容性测试
# ============================================================================


class TestFormatCompatibility:
    """KingdeeRealAdapter 的 _map_* 方法输出应与 MockAdapter 完全一致"""

    def test_product_fields(self):
        """产品字段结构一致"""
        row = {
            "FNumber": "P001",
            "FName": "玫瑰焕颜精华液",
            "FCategory": "精华",
            "FPrice": 298.0,
            "FSpecification": "30ml",
            "FDescription": "玫瑰精油,透明质酸",
            "FAuxProperty": "所有肤质",
        }
        mapped = KingdeeRealAdapter._map_product(row)
        assert mapped["id"] == "P001"
        assert mapped["name"] == "玫瑰焕颜精华液"
        assert mapped["category"] == "精华"
        assert mapped["price"] == 298.0
        assert mapped["specs"] == "30ml"
        assert mapped["ingredients"] == "玫瑰精油,透明质酸"
        assert mapped["suitable"] == "所有肤质"

    def test_product_fields_missing(self):
        """缺失字段时使用默认值"""
        mapped = KingdeeRealAdapter._map_product({})
        assert mapped["id"] == ""
        assert mapped["name"] == ""
        assert mapped["price"] == 0.0

    def test_product_fields_none_price(self):
        """价格为 None 时安全转换"""
        row = {"FPrice": None}
        mapped = KingdeeRealAdapter._map_product(row)
        assert mapped["price"] == 0.0

    def test_inventory_fields(self):
        """库存字段结构一致"""
        row = {
            "FMaterialId": {"FNumber": "P001", "FName": "玫瑰焕颜精华液"},
            "FQty": 1200,
            "FStockId": {"FName": "上海仓"},
            "FDate": "2026-05-30",
        }
        mapped = KingdeeRealAdapter._map_inventory(row)
        assert mapped["product_id"] == "P001"
        assert mapped["product_name"] == "玫瑰焕颜精华液"
        assert mapped["stock"] == 1200
        assert mapped["warehouse"] == "上海仓"
        assert mapped["updated"] == "2026-05-30"

    def test_inventory_fields_string_refs(self):
        """库存行中引用为字符串时安全处理"""
        row = {
            "FMaterialId": "P001",
            "FQty": 50,
            "FStockId": "仓库A",
            "FDate": "2026-06-01",
        }
        mapped = KingdeeRealAdapter._map_inventory(row)
        assert mapped["product_id"] == ""
        assert mapped["stock"] == 50

    def test_order_fields(self):
        """订单字段结构一致"""
        row = {
            "FBillNo": "ORD001",
            "FCUSTID": {"FNumber": "C001", "FName": "王女士"},
            "FOrderAmount": 596.0,
            "FStatus": "已发货",
            "FTrackingNo": "SF123456",
            "FDate": "2026-05-28",
            "BillEntry": [
                {"FMaterialId": {"FName": "玫瑰焕颜精华液"}},
                {"FMaterialId": {"FName": "绿茶控油洁面乳"}},
            ],
        }
        mapped = KingdeeRealAdapter._map_order(row)
        assert mapped["order_id"] == "ORD001"
        assert mapped["customer_id"] == "C001"
        assert mapped["customer_name"] == "王女士"
        assert mapped["total"] == 596.0
        assert mapped["status"] == "已发货"
        assert mapped["tracking"] == "SF123456"
        assert mapped["items"] == ["玫瑰焕颜精华液", "绿茶控油洁面乳"]

    def test_order_empty_entries(self):
        """订单无明细行时返回空列表"""
        row = {"FBillNo": "ORD002", "BillEntry": []}
        mapped = KingdeeRealAdapter._map_order(row)
        assert mapped["items"] == []

    def test_order_string_bill_entry(self):
        """订单明细为字符串时安全处理"""
        row = {"FBillNo": "ORD003", "BillEntry": "invalid"}
        mapped = KingdeeRealAdapter._map_order(row)
        assert mapped["items"] == []

    def test_customer_fields(self):
        """客户字段结构一致"""
        row = {
            "FNumber": "C001",
            "FName": "王女士",
            "FPhoneNumber": "138****1234",
            "FVIPLevel": "VIP",
            "FAddress": "上海市浦东新区",
        }
        mapped = KingdeeRealAdapter._map_customer(row)
        assert mapped["id"] == "C001"
        assert mapped["name"] == "王女士"
        assert mapped["phone"] == "138****1234"
        assert mapped["level"] == "VIP"
        assert mapped["address"] == "上海市浦东新区"

    def test_customer_default_level(self):
        """客户等级缺失时使用默认值"""
        mapped = KingdeeRealAdapter._map_customer({"FNumber": "C999"})
        assert mapped["level"] == "普通"


# ============================================================================
# 2. MockAdapter 与 RealAdapter 返回格式一致性测试
# ============================================================================


class TestMockVsRealFormat:
    """确保两种适配器返回完全相同的字段集"""

    def test_product_keys_match(self):
        real_keys = {"id", "name", "category", "price", "specs", "ingredients", "suitable"}

        # MockAdapter 的返回字段
        mock_row = {
            "id": "P001",
            "name": "test",
            "category": "test",
            "price": 1.0,
            "specs": "test",
            "ingredients": "test",
            "suitable": "test",
        }
        assert set(mock_row.keys()) == real_keys

    def test_inventory_keys_match(self):
        real_keys = {"product_id", "product_name", "stock", "warehouse", "updated"}
        mapped = KingdeeRealAdapter._map_inventory(
            {
                "FMaterialId": {"FNumber": "X", "FName": "Y"},
                "FQty": 1,
                "FStockId": {"FName": "Z"},
                "FDate": "D",
            }
        )
        assert set(mapped.keys()) == real_keys

    def test_order_keys_match(self):
        real_keys = {
            "order_id",
            "customer_id",
            "customer_name",
            "total",
            "status",
            "tracking",
            "created",
            "items",
        }
        mapped = KingdeeRealAdapter._map_order(
            {
                "FBillNo": "X",
                "FCUSTID": {"FNumber": "A", "FName": "B"},
                "FOrderAmount": 1,
                "FStatus": "S",
                "FTrackingNo": "T",
                "FDate": "D",
                "BillEntry": [],
            }
        )
        assert set(mapped.keys()) == real_keys

    def test_customer_keys_match(self):
        real_keys = {"id", "name", "phone", "level", "address"}
        mapped = KingdeeRealAdapter._map_customer(
            {
                "FNumber": "X",
                "FName": "Y",
                "FPhoneNumber": "Z",
                "FVIPLevel": "V",
                "FAddress": "A",
            }
        )
        assert set(mapped.keys()) == real_keys


# ============================================================================
# 3. Token 过期与自动刷新测试
# ============================================================================


class TestTokenManagement:
    """测试 Token 缓存、过期检测、自动刷新"""

    def test_token_not_valid_when_none(self, real_adapter: KingdeeRealAdapter):
        assert real_adapter._token_is_valid() is False

    def test_token_not_valid_when_expired(self, real_adapter: KingdeeRealAdapter):
        real_adapter._token = "test_token"
        real_adapter._token_expires = time.time() - 100  # 已过期
        assert real_adapter._token_is_valid() is False

    def test_token_not_valid_within_buffer(self, real_adapter: KingdeeRealAdapter):
        """过期前 5 分钟内应视为无效（触发刷新）"""
        real_adapter._token = "test_token"
        # 过期时间 = 当前时间 + 200s（小于 300s 缓冲）
        real_adapter._token_expires = time.time() + 200
        assert real_adapter._token_is_valid() is False

    def test_token_valid_when_far_from_expiry(self, real_adapter: KingdeeRealAdapter):
        real_adapter._token = "test_token"
        # 过期时间 = 当前时间 + 1 小时（远大于 300s 缓冲）
        real_adapter._token_expires = time.time() + 3600
        assert real_adapter._token_is_valid() is True

    @pytest.mark.asyncio
    async def test_ensure_token_refreshes_when_invalid(self, real_adapter: KingdeeRealAdapter):
        """_ensure_token 在 token 无效时应调用 _refresh_token"""
        mock_resp = _make_http_response(200, {"Token": "new_token_123"})
        real_adapter._client = _make_mock_client(post_return=mock_resp)

        await real_adapter._ensure_token()
        assert real_adapter._token == "new_token_123"
        assert real_adapter._token_expires > time.time()

    @pytest.mark.asyncio
    async def test_ensure_token_skips_when_valid(self, real_adapter: KingdeeRealAdapter):
        """token 有效时不应发起 HTTP 请求"""
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        mock_client = _make_mock_client()
        real_adapter._client = mock_client

        await real_adapter._ensure_token()
        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_token_retries_on_failure(self, real_adapter: KingdeeRealAdapter):
        """token 获取失败时应重试（最多 3 次）"""
        mock_client = _make_mock_client(post_side_effect=httpx.ConnectError("connection refused"))
        real_adapter._client = mock_client

        with pytest.raises(httpx.ConnectError):
            await real_adapter._ensure_token()
        # 3 次重试 = 3 次调用
        assert mock_client.post.call_count == 3


# ============================================================================
# 4. 重试逻辑测试
# ============================================================================


class TestRetryLogic:
    """测试指数退避重试机制"""

    @pytest.mark.asyncio
    async def test_api_call_retries_on_server_error(self, real_adapter: KingdeeRealAdapter):
        """500 错误应触发重试"""
        # 先让 token 有效
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        error_resp = _make_http_response(500)
        ok_resp = _make_http_response(200, {"Data": {"Rows": []}})

        real_adapter._client = _make_mock_client(
            post_side_effect=[
                httpx.HTTPStatusError("500", request=MagicMock(), response=error_resp),
                ok_resp,
            ]
        )

        result = await real_adapter._api_call("BD_MATERIAL", "BillQuery", {"FormId": "BD_MATERIAL"})
        assert result == {"Data": {"Rows": []}}

    @pytest.mark.asyncio
    async def test_api_call_refreshes_token_on_401(self, real_adapter: KingdeeRealAdapter):
        """401 错误应触发 token 刷新并重试"""
        real_adapter._token = "old_expired_token"
        real_adapter._token_expires = time.time() + 3600

        error_resp = _make_http_response(401)
        token_resp = _make_http_response(200, {"Token": "refreshed_token"})
        ok_resp = _make_http_response(200, {"Data": {"Rows": [{"FName": "test"}]}})

        # 第一次调用返回 401，第二次（刷新 token）返回新 token，第三次（重试）返回成功
        real_adapter._client = _make_mock_client(
            post_side_effect=[
                httpx.HTTPStatusError("401", request=MagicMock(), response=error_resp),
                token_resp,
                ok_resp,
            ]
        )

        result = await real_adapter._api_call("BD_MATERIAL", "BillQuery", {"FormId": "BD_MATERIAL"})
        assert result["Data"]["Rows"][0]["FName"] == "test"
        assert real_adapter._token == "refreshed_token"

    @pytest.mark.asyncio
    async def test_api_call_fails_after_max_retries(self, real_adapter: KingdeeRealAdapter):
        """持续 500 错误时，重试耗尽后应抛出异常"""
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        error_resp = _make_http_response(500)
        real_adapter._client = _make_mock_client(
            post_side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=error_resp)
        )

        with pytest.raises(httpx.HTTPStatusError):
            await real_adapter._api_call("BD_MATERIAL", "BillQuery", {"FormId": "BD_MATERIAL"})
        assert real_adapter._client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_api_call_network_error_retries(self, real_adapter: KingdeeRealAdapter):
        """网络异常应触发重试"""
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        real_adapter._client = _make_mock_client(post_side_effect=httpx.ConnectError("DNS failed"))

        with pytest.raises(httpx.ConnectError):
            await real_adapter._api_call("BD_MATERIAL", "BillQuery", {"FormId": "BD_MATERIAL"})
        assert real_adapter._client.post.call_count == 3


# ============================================================================
# 5. 重试装饰器单元测试
# ============================================================================


class TestExponentialBackoffDecorator:
    """测试 _exponential_backoff 装饰器本身"""

    @pytest.mark.asyncio
    async def test_succeeds_without_retry(self):
        call_count = 0

        @_exponential_backoff(max_attempts=3, base_delay=0.01, exceptions=(ValueError,))
        async def ok_func():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await ok_func()
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_specified_exception(self):
        call_count = 0

        @_exponential_backoff(max_attempts=3, base_delay=0.01, exceptions=(ValueError,))
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("not yet")
            return "done"

        result = await flaky_func()
        assert result == "done"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_does_not_catch_unspecified_exception(self):
        @_exponential_backoff(max_attempts=3, base_delay=0.01, exceptions=(ValueError,))
        async def wrong_exc():
            raise TypeError("wrong type")

        with pytest.raises(TypeError):
            await wrong_exc()

    @pytest.mark.asyncio
    async def test_exhausts_retries(self):
        call_count = 0

        @_exponential_backoff(max_attempts=3, base_delay=0.01, exceptions=(RuntimeError,))
        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("always")

        with pytest.raises(RuntimeError):
            await always_fail()
        assert call_count == 3


# ============================================================================
# 6. 分页查询测试
# ============================================================================


class TestPagedQuery:
    """测试 _paged_query 的分页合并逻辑"""

    @pytest.mark.asyncio
    async def test_single_page(self, real_adapter: KingdeeRealAdapter):
        """单页结果（不足一页）"""
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        page_data = [{"FName": "A"}, {"FName": "B"}]
        resp = _make_http_response(200, {"Data": {"Rows": page_data}})
        real_adapter._client = _make_mock_client(post_return=resp)

        rows = await real_adapter._paged_query("BD_MATERIAL", "FName='A'")
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_multi_page(self, real_adapter: KingdeeRealAdapter):
        """多页结果应自动合并"""
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        page1 = [{"FName": f"item_{i}"} for i in range(100)]
        page2 = [{"FName": f"item_{i}"} for i in range(100, 120)]

        resp1 = _make_http_response(200, {"Data": {"Rows": page1}})
        resp2 = _make_http_response(200, {"Data": {"Rows": page2}})

        real_adapter._client = _make_mock_client(post_side_effect=[resp1, resp2])

        rows = await real_adapter._paged_query("BD_MATERIAL", "")
        assert len(rows) == 120

    @pytest.mark.asyncio
    async def test_empty_result(self, real_adapter: KingdeeRealAdapter):
        """空结果"""
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        resp = _make_http_response(200, {"Data": {"Rows": []}})
        real_adapter._client = _make_mock_client(post_return=resp)

        rows = await real_adapter._paged_query("BD_MATERIAL", "FName='不存在'")
        assert rows == []

    @pytest.mark.asyncio
    async def test_top_row_limit(self, real_adapter: KingdeeRealAdapter):
        """top_row_count 限制返回数量"""
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        page = [{"FName": f"item_{i}"} for i in range(50)]
        resp = _make_http_response(200, {"Data": {"Rows": page}})
        real_adapter._client = _make_mock_client(post_return=resp)

        rows = await real_adapter._paged_query("BD_MATERIAL", "", top_row_count=10)
        assert len(rows) == 10


# ============================================================================
# 7. query_product 集成测试
# ============================================================================


class TestQueryProduct:
    """测试 query_product 的完整流程"""

    @pytest.mark.asyncio
    async def test_returns_mapped_products(self, real_adapter: KingdeeRealAdapter):
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        api_rows = [
            {
                "FNumber": "P001",
                "FName": "玫瑰焕颜精华液",
                "FCategory": "精华",
                "FPrice": 298.0,
                "FSpecification": "30ml",
                "FDescription": "玫瑰精油",
                "FAuxProperty": "所有肤质",
            }
        ]
        resp = _make_http_response(200, {"Data": {"Rows": api_rows}})
        real_adapter._client = _make_mock_client(post_return=resp)

        products = await real_adapter.query_product("玫瑰")
        assert len(products) == 1
        assert products[0]["id"] == "P001"
        assert products[0]["name"] == "玫瑰焕颜精华液"

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self, real_adapter: KingdeeRealAdapter):
        """查询失败时返回空列表（降级处理）"""
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        real_adapter._client = _make_mock_client(post_side_effect=httpx.ConnectError("timeout"))

        products = await real_adapter.query_product("test")
        assert products == []


# ============================================================================
# 8. query_order 权限检查测试
# ============================================================================


class TestQueryOrder:
    @pytest.mark.asyncio
    async def test_rejects_empty_params(self, real_adapter: KingdeeRealAdapter):
        """无参数查询应被拒绝"""
        result = await real_adapter.query_order()
        assert result == []

    @pytest.mark.asyncio
    async def test_query_by_order_id(self, real_adapter: KingdeeRealAdapter):
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        api_rows = [
            {
                "FBillNo": "ORD001",
                "FCUSTID": {"FNumber": "C001", "FName": "王女士"},
                "FOrderAmount": 596.0,
                "FStatus": "已发货",
                "FTrackingNo": "SF123",
                "FDate": "2026-05-28",
                "BillEntry": [{"FMaterialId": {"FName": "精华液"}}],
            }
        ]
        resp = _make_http_response(200, {"Data": {"Rows": api_rows}})
        real_adapter._client = _make_mock_client(post_return=resp)

        orders = await real_adapter.query_order(order_id="ORD001")
        assert len(orders) == 1
        assert orders[0]["order_id"] == "ORD001"


# ============================================================================
# 9. query_customer 集成测试
# ============================================================================


class TestQueryCustomer:
    @pytest.mark.asyncio
    async def test_returns_none_for_empty_id(self, real_adapter: KingdeeRealAdapter):
        result = await real_adapter.query_customer("")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_mapped_customer(self, real_adapter: KingdeeRealAdapter):
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        api_rows = [
            {
                "FNumber": "C001",
                "FName": "王女士",
                "FPhoneNumber": "138****1234",
                "FVIPLevel": "VIP",
                "FAddress": "上海",
            }
        ]
        resp = _make_http_response(200, {"Data": {"Rows": api_rows}})
        real_adapter._client = _make_mock_client(post_return=resp)

        customer = await real_adapter.query_customer("C001")
        assert customer is not None
        assert customer["name"] == "王女士"
        assert customer["level"] == "VIP"

    @pytest.mark.asyncio
    async def test_returns_none_for_not_found(self, real_adapter: KingdeeRealAdapter):
        real_adapter._token = "valid_token"
        real_adapter._token_expires = time.time() + 3600

        resp = _make_http_response(200, {"Data": {"Rows": []}})
        real_adapter._client = _make_mock_client(post_return=resp)

        customer = await real_adapter.query_customer("C999")
        assert customer is None


# ============================================================================
# 10. close 资源清理测试
# ============================================================================


class TestClose:
    @pytest.mark.asyncio
    async def test_close_cleans_up(self, real_adapter: KingdeeRealAdapter):
        real_adapter._token = "test_token"
        real_adapter._token_expires = time.time() + 3600

        mock_client = AsyncMock()
        mock_client.is_closed = False
        real_adapter._client = mock_client

        await real_adapter.close()
        mock_client.aclose.assert_called_once()
        assert real_adapter._token is None
        assert real_adapter._token_expires == 0
