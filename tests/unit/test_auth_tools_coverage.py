"""
测试 auth.service 和 tools.erp_tools（补齐覆盖率至 80%+）
"""

import asyncio
import hashlib
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─── auth.service ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_auth_globals(monkeypatch):
    """Reset auth.service globals before each test to prevent cross-test/state leakage."""
    import auth.service

    monkeypatch.setattr(auth.service, "_revoked_jtis", set())
    dl = auth.service._TokenDenylist()
    dl._use_redis = False
    monkeypatch.setattr(auth.service, "_denylist", dl)


class TestPasswordHashing:
    """密码哈希与验证"""

    def test_hash_password_format(self):
        from auth.service import hash_password

        h = hash_password("test123")
        assert "$" in h
        salt, dk = h.split("$", 1)
        assert len(salt) == 32  # 16 bytes hex
        assert len(dk) == 64  # sha256 hex

    def test_hash_password_unique_salt(self):
        from auth.service import hash_password

        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # salt differs

    def test_verify_password_correct(self):
        from auth.service import hash_password, verify_password

        h = hash_password("mypassword")
        assert verify_password("mypassword", h) is True

    def test_verify_password_wrong(self):
        from auth.service import hash_password, verify_password

        h = hash_password("mypassword")
        assert verify_password("wrongpassword", h) is False

    def test_verify_password_invalid_format(self):
        from auth.service import verify_password

        assert verify_password("x", "no_dollar_sign") is False

    def test_verify_password_old_iteration(self):
        """旧迭代次数（100,000）也能验证"""
        from auth.service import verify_password

        salt = os.urandom(16).hex()
        dk = hashlib.pbkdf2_hmac("sha256", b"oldpw", salt.encode(), 100000)
        h = f"{salt}${dk.hex()}"
        assert verify_password("oldpw", h) is True

    def test_verify_password_empty(self):
        from auth.service import verify_password

        assert verify_password("", "") is False


class TestTokenCreation:
    """JWT token 生成"""

    @patch("auth.service._config")
    def test_create_token_has_required_claims(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        mock_config.JWT_EXPIRE_HOURS = 72
        import jwt as pyjwt

        from auth.service import create_token

        token = create_token(1, "testuser", "customer")
        payload = pyjwt.decode(
            token, "test_secret_key_at_least_32_chars_long!!", algorithms=["HS256"]
        )
        assert payload["sub"] == "1"
        assert payload["username"] == "testuser"
        assert payload["role"] == "customer"
        assert "jti" in payload
        assert "exp" in payload

    @patch("auth.service._config")
    def test_create_access_token_type(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        mock_config.JWT_ACCESS_EXPIRE_HOURS = 2
        import jwt as pyjwt

        from auth.service import create_access_token

        token = create_access_token(1, "user", "admin")
        payload = pyjwt.decode(
            token, "test_secret_key_at_least_32_chars_long!!", algorithms=["HS256"]
        )
        assert payload["type"] == "access"

    @patch("auth.service._config")
    def test_create_token_no_secret_raises(self, mock_config):
        mock_config.JWT_SECRET = ""
        from auth.service import _create_token

        with pytest.raises(ValueError, match="JWT_SECRET"):
            _create_token(1, "u", "r", None, 1)


class TestTokenDecode:
    """JWT token 解码与吊销检查"""

    @patch("auth.service._config")
    def test_decode_valid_token(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        mock_config.JWT_EXPIRE_HOURS = 72
        from auth.service import create_token, decode_token

        token = create_token(42, "alice", "admin")
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == 42  # int after conversion
        assert payload["username"] == "alice"

    @patch("auth.service._config")
    def test_decode_expired_token(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        import jwt as pyjwt

        from auth.service import decode_token

        token = pyjwt.encode(
            {"sub": "1", "exp": int(time.time()) - 10},
            "test_secret_key_at_least_32_chars_long!!",
            algorithm="HS256",
        )
        assert decode_token(token) is None

    @patch("auth.service._config")
    def test_decode_no_secret_returns_none(self, mock_config):
        mock_config.JWT_SECRET = ""
        from auth.service import decode_token

        assert decode_token("any.token.here") is None

    @patch("auth.service._config")
    def test_decode_revoked_jti(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        mock_config.JWT_EXPIRE_HOURS = 72
        from auth.service import _revoked_jtis, create_token, decode_token

        token = create_token(1, "u", "r")
        # Manually decode to get jti
        import jwt as pyjwt

        jti = pyjwt.decode(token, "test_secret_key_at_least_32_chars_long!!", algorithms=["HS256"])[
            "jti"
        ]
        _revoked_jtis.add(jti)
        try:
            assert decode_token(token) is None
        finally:
            _revoked_jtis.discard(jti)


class TestTokenDecodeAsync:
    """异步 token 解码"""

    @patch("auth.service._config")
    @pytest.mark.asyncio
    async def test_decode_async_valid(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        mock_config.JWT_EXPIRE_HOURS = 72
        mock_config.REDIS_JWT_PREFIX = "csai:jwt:blacklist:"
        from auth.service import create_token, decode_token_async

        token = create_token(1, "u", "r")
        result = await decode_token_async(token)
        assert result is not None
        assert result["sub"] == 1

    @patch("auth.service._config")
    @pytest.mark.asyncio
    async def test_decode_async_no_secret(self, mock_config):
        mock_config.JWT_SECRET = ""
        from auth.service import decode_token_async

        result = await decode_token_async("bad")
        assert result is None


class TestTokenRevocation:
    """Token 吊销"""

    @patch("auth.service._config")
    @pytest.mark.asyncio
    async def test_revoke_token(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        mock_config.JWT_EXPIRE_HOURS = 72
        mock_config.REDIS_JWT_PREFIX = "csai:jwt:blacklist:"
        from auth.service import _revoked_jtis, create_token, decode_token, revoke_token

        token = create_token(1, "u", "r")
        import jwt as pyjwt

        jti = pyjwt.decode(token, "test_secret_key_at_least_32_chars_long!!", algorithms=["HS256"])[
            "jti"
        ]

        result = await revoke_token(token)
        assert result is True
        assert jti in _revoked_jtis
        # After revocation, decode should fail (memory path)
        try:
            assert decode_token(token) is None
        finally:
            _revoked_jtis.discard(jti)

    @patch("auth.service._config")
    @pytest.mark.asyncio
    async def test_revoke_invalid_token(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        from auth.service import revoke_token

        result = await revoke_token("bad")
        assert result is False


class TestTokenDenylist:
    """内存模式 Token 黑名单"""

    @pytest.mark.asyncio
    async def test_memory_denylist_add_contains(self):
        from auth.service import _TokenDenylist

        dl = _TokenDenylist()
        dl._use_redis = False  # force memory mode
        await dl.add("jti1", 60)
        result = await dl.contains("jti1")
        assert result is True
        result = await dl.contains("jti2")
        assert result is False

    @pytest.mark.asyncio
    async def test_memory_denylist_eviction(self):
        from auth.service import _TokenDenylist

        dl = _TokenDenylist()
        dl._use_redis = False
        dl.MAX_DENYLIST_SIZE = 5
        for i in range(6):
            await dl.add(f"jti_{i}", 60)
        # Should have evicted some entries
        assert len(dl._memory_set) <= 5


class TestUserFunctions:
    """用户注册、认证、查询"""

    @patch("auth.service._config")
    def test_register_user_success(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        from auth.service import register_user

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None
        mock_user = MagicMock()
        mock_user.id = 1
        mock_session.refresh = MagicMock()
        with (
            patch("auth.service.get_db_session", return_value=mock_session),
            patch("auth.service.User", return_value=mock_user),
        ):
            result = register_user("newuser", "password123")
            assert result["success"] is True
            assert result["username"] == "newuser"

    @patch("auth.service._config")
    def test_register_user_duplicate(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        from auth.service import register_user

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = MagicMock()
        with patch("auth.service.get_db_session", return_value=mock_session):
            result = register_user("existing", "pw")
            assert result["success"] is False
            assert "已存在" in result["error"]

    @patch("auth.service._config")
    def test_register_user_db_error(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        from sqlalchemy.exc import SQLAlchemyError

        from auth.service import register_user

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None
        mock_session.commit.side_effect = SQLAlchemyError("db error")
        with (
            patch("auth.service.get_db_session", return_value=mock_session),
            patch("auth.service.User", return_value=MagicMock()),
        ):
            result = register_user("u", "p")
            assert result["success"] is False

    @patch("auth.service._config")
    @pytest.mark.asyncio
    async def test_authenticate_user_success(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        mock_config.JWT_EXPIRE_HOURS = 72
        mock_config.JWT_REFRESH_EXPIRE_HOURS = 168
        mock_config.REDIS_JWT_PREFIX = "csai:jwt:blacklist:"
        from auth.service import authenticate_user, hash_password

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.username = "alice"
        mock_user.role = "customer"
        mock_user.display_name = "Alice"
        mock_user.password_hash = hash_password("correct")
        mock_user.is_active = True

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = mock_user
        with patch("auth.service.get_db_session", return_value=mock_session):
            result = await authenticate_user("alice", "correct")
            assert result is not None
            assert result["username"] == "alice"
            assert "token" in result
            assert "refresh_token" in result

    @patch("auth.service._config")
    @pytest.mark.asyncio
    async def test_authenticate_user_wrong_password(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        from auth.service import authenticate_user, hash_password

        mock_user = MagicMock()
        mock_user.password_hash = hash_password("correct")
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = mock_user
        with patch("auth.service.get_db_session", return_value=mock_session):
            result = await authenticate_user("alice", "wrong")
            assert result is None

    @patch("auth.service._config")
    @pytest.mark.asyncio
    async def test_authenticate_user_not_found(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        from auth.service import authenticate_user

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None
        with patch("auth.service.get_db_session", return_value=mock_session):
            result = await authenticate_user("nobody", "pw")
            assert result is None

    @patch("auth.service._config")
    def test_get_current_user_valid(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        mock_config.JWT_EXPIRE_HOURS = 72
        from auth.service import create_token, get_current_user

        token = create_token(5, "bob", "agent")
        mock_user = MagicMock()
        mock_user.is_active = True
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = mock_user
        with patch("auth.service.get_db_session", return_value=mock_session):
            user = get_current_user(token)
            assert user is not None

    @patch("auth.service._config")
    def test_get_current_user_invalid_token(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        from auth.service import get_current_user

        assert get_current_user("bad.token") is None

    def test_get_user_id_from_request(self):
        from auth.service import get_user_id_from_request

        mock_request = MagicMock()
        mock_request.headers = {"Authorization": "Bearer some.jwt.token"}
        # decode_token will fail for "some.jwt.token" → None
        result = get_user_id_from_request(mock_request)
        assert result is None

    def test_get_user_id_from_request_no_header(self):
        from auth.service import get_user_id_from_request

        mock_request = MagicMock()
        mock_request.headers = {}
        result = get_user_id_from_request(mock_request)
        assert result is None

    @patch("auth.service._config")
    def test_init_default_admin_creates(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        from auth.service import init_default_admin

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None
        with (
            patch("auth.service.get_db_session", return_value=mock_session),
            patch("auth.service.User") as MockUser,
        ):
            mock_admin = MagicMock()
            MockUser.return_value = mock_admin
            init_default_admin()
            mock_session.add.assert_called_once_with(mock_admin)
            mock_session.commit.assert_called_once()

    @patch("auth.service._config")
    def test_init_default_admin_exists(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        from auth.service import init_default_admin

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = MagicMock()
        with patch("auth.service.get_db_session", return_value=mock_session):
            init_default_admin()
            mock_session.add.assert_not_called()


class TestRevokeUserTokens:
    """用户 Token 吊销"""

    @patch("auth.service._config")
    @pytest.mark.asyncio
    async def test_revoke_without_redis(self, mock_config):
        mock_config.JWT_SECRET = "test_secret_key_at_least_32_chars_long!!"
        mock_config.REDIS_JWT_PREFIX = "csai:jwt:blacklist:"
        from auth.service import revoke_user_tokens

        # Patch _denylist to not use redis
        with patch("auth.service._denylist") as mock_dl:
            mock_dl._use_redis = False
            result = await revoke_user_tokens(1)
            assert result == 0


# ─── tools.erp_tools ─────────────────────────────────────────────


class TestERPTools:
    """ERP 工具注册与执行"""

    @pytest.fixture
    def mock_erp(self):
        erp = AsyncMock()
        return erp

    @pytest.fixture
    def registry(self, mock_erp):
        from tools.erp_tools import create_erp_tools

        return create_erp_tools(mock_erp)

    @pytest.mark.asyncio
    async def test_query_product_found(self, registry, mock_erp):
        mock_erp.query_product.return_value = [
            {
                "name": "面霜",
                "category": "护肤",
                "price": 199,
                "specs": "50ml",
                "ingredients": "玻尿酸",
                "suitable": "干性",
            }
        ]
        tools = registry.list_tools()
        assert "query_product" in tools
        result = await registry.execute("query_product", {"keyword": "面霜"})
        assert "面霜" in result
        assert "199" in result

    @pytest.mark.asyncio
    async def test_query_product_empty(self, registry, mock_erp):
        mock_erp.query_product.return_value = []
        result = await registry.execute("query_product", {"keyword": "不存在"})
        assert "未找到" in result

    @pytest.mark.asyncio
    async def test_query_inventory_found(self, registry, mock_erp):
        mock_erp.query_inventory.return_value = [
            {"product_name": "精华液", "stock": 500, "warehouse": "A仓", "updated": "2026-06-10"}
        ]
        result = await registry.execute("query_inventory", {"product_id": "P001", "keyword": ""})
        assert "精华液" in result
        assert "500" in result

    @pytest.mark.asyncio
    async def test_query_inventory_empty(self, registry, mock_erp):
        mock_erp.query_inventory.return_value = []
        result = await registry.execute("query_inventory", {"product_id": "", "keyword": "xxx"})
        assert "未找到" in result

    @pytest.mark.asyncio
    async def test_query_order_found(self, registry, mock_erp):
        mock_erp.query_order.return_value = [
            {
                "order_id": "ORD001",
                "customer_name": "张三",
                "status": "已发货",
                "total": 599,
                "tracking": "SF123456",
                "created": "2026-06-01",
            }
        ]
        result = await registry.execute("query_order", {"order_id": "ORD001", "customer_id": ""})
        assert "ORD001" in result
        assert "已发货" in result

    @pytest.mark.asyncio
    async def test_query_order_empty(self, registry, mock_erp):
        mock_erp.query_order.return_value = []
        result = await registry.execute("query_order", {"order_id": "NONE", "customer_id": ""})
        assert "未找到" in result

    @pytest.mark.asyncio
    async def test_query_order_limit_5(self, registry, mock_erp):
        mock_erp.query_order.return_value = [
            {"order_id": f"ORD{i}", "customer_name": "C", "status": "S", "total": 100}
            for i in range(10)
        ]
        result = await registry.execute("query_order", {"order_id": "", "customer_id": "C001"})
        # Only first 5 orders shown
        assert result.count("ORD") == 5

    @pytest.mark.asyncio
    async def test_query_customer_found(self, registry, mock_erp):
        mock_erp.query_customer.return_value = {
            "name": "李四",
            "phone": "13800000000",
            "level": "VIP",
            "total_spent": 10000,
            "total_orders": 25,
            "address": "北京市",
        }
        result = await registry.execute("query_customer", {"customer_id": "C001"})
        assert "李四" in result
        assert "VIP" in result

    @pytest.mark.asyncio
    async def test_query_customer_not_found(self, registry, mock_erp):
        mock_erp.query_customer.return_value = None
        result = await registry.execute("query_customer", {"customer_id": "C999"})
        assert "未找到" in result

    def test_registry_has_4_tools(self, registry):
        tools = registry.list_tools()
        assert len(tools) == 4
        assert set(tools) == {"query_product", "query_inventory", "query_order", "query_customer"}

    @pytest.mark.asyncio
    async def test_execute_nonexistent_tool(self, registry):
        result = await registry.execute("nonexistent", {})
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_execute_tool_exception(self, registry, mock_erp):
        mock_erp.query_product.side_effect = RuntimeError("DB error")
        result = await registry.execute("query_product", {"keyword": "x"})
        assert "失败" in result
