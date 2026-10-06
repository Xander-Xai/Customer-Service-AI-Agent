"""
认证业务逻辑（v5.4）
- Argon2id 密码哈希（v5.4 升级，OWASP 2023 推荐）+ PBKDF2-SHA256 向后兼容（600K 迭代）
- PyJWT token 生成/验证（HS256 + 算法白名单）
- 用户 CRUD
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import jwt
import redis as _redis_mod
from sqlalchemy.exc import SQLAlchemyError

from core.logger import get_logger
from db.database import get_db_session
from db.models import User

logger = get_logger("auth.service")

# 本地 LRU 缓存：已吊销的 JTI 集合（Redis 不可用时的快速拒绝层）
# v6.2: 添加容量限制，防止内存泄漏
_MAX_REVOKED_JTIS = 10000
_revoked_jtis: set = set()

# JWT 配置（v4.0 安全修复：从 config 读取，不再有硬编码默认值）
from core import config as _config  # noqa: E402

_JWT_ALGORITHM = "HS256"
_JWT_EXPIRE_HOURS = _config.JWT_EXPIRE_HOURS


class _TokenDenylist:
    """JWT 黑名单：优先使用 Redis，不可用时回退到内存 set"""

    MAX_DENYLIST_SIZE = 10000

    def __init__(self):
        self._use_redis = False
        self._redis = None
        self._memory_set: set = set()
        self._prefix = getattr(_config, "REDIS_JWT_PREFIX", "csai:jwt:blacklist:")
        try:
            import redis as _redis_lib

            redis_url = getattr(_config, "REDIS_URL", "redis://localhost:6379")
            self._redis = _redis_lib.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=2,
                socket_connect_timeout=2,
            )
            self._redis.ping()
            self._use_redis = True
            logger.info("JWT 黑名单已启用 Redis 后端")
        except (_redis_mod.RedisError, OSError, TypeError) as e:
            logger.warning(f"Redis 不可用，JWT 黑名单回退到内存模式: {e}")
            # 生产环境不使用 Redis 时发出警告
            app_mode = os.getenv("APP_MODE", "").lower()
            if app_mode == "prod":
                logger.warning(
                    "JWT 黑名单运行在生产环境但未使用 Redis，"
                    f"内存模式最大容量为 {self.MAX_DENYLIST_SIZE} 条，"
                    "重启后黑名单将丢失"
                )

    async def add(self, jti: str, ttl_seconds: int) -> None:
        """将 jti 加入黑名单"""
        if self._use_redis:
            key = f"{self._prefix}{jti}"
            await asyncio.to_thread(self._redis.set, key, "1", ex=max(ttl_seconds, 1))
        else:
            # 内存模式：检查容量，超过上限时清理旧条目
            if len(self._memory_set) >= self.MAX_DENYLIST_SIZE:
                evict_count = self.MAX_DENYLIST_SIZE // 4
                for _ in range(evict_count):
                    self._memory_set.pop()
            self._memory_set.add(jti)

    async def contains(self, jti: str) -> bool:
        """检查 jti 是否在黑名单中"""
        if self._use_redis:
            key = f"{self._prefix}{jti}"
            return (await asyncio.to_thread(self._redis.exists, key)) > 0
        return jti in self._memory_set


_denylist = _TokenDenylist()


def hash_password(password: str) -> str:
    """
    密码哈希（v5.4: Argon2id, OWASP 2023推荐标准）

    Argon2id优势：
    - 抗GPU/ASIC攻击能力更强
    - 内存硬函数，增加暴力破解成本
    - 同时抵抗侧信道攻击和时间-空间权衡攻击

    参数配置：
    - time_cost=3: 迭代次数
    - memory_cost=65536: 内存使用64MB
    - parallelism=4: 并行度
    - hash_len=32: 输出长度32字节
    - salt_len=16: 盐长度16字节

    迁移策略：
    - 新用户密码使用Argon2id
    - 旧用户登录验证成功后自动重新哈希
    - 向后兼容PBKDF2-SHA256格式
    """
    try:
        from argon2 import PasswordHasher

        # v5.4: 使用Argon2id（默认模式）
        ph = PasswordHasher(
            time_cost=3,
            memory_cost=65536,  # 64 MB
            parallelism=4,
            hash_len=32,
            salt_len=16,
        )

        return ph.hash(password)
    except ImportError:
        # 降级方案：如果argon2-cffi未安装，回退到PBKDF2-SHA256
        logger.warning("argon2-cffi 未安装，回退到 PBKDF2-SHA256")
        import os

        salt = os.urandom(16).hex()
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000)
        return f"{salt}${dk.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """
    验证密码（支持Argon2id和PBKDF2-SHA256）

    验证流程：
    1. 尝试Argon2id验证（新格式）
    2. 失败则尝试PBKDF2-SHA256（旧格式，向后兼容）
    3. PBKDF2验证成功后标记需要迁移

    Args:
        password: 明文密码
        password_hash: 存储的哈希值

    Returns:
        bool: 密码是否匹配
    """
    # v5.4: 优先尝试Argon2id验证
    if password_hash.startswith("$argon2"):
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import VerificationError

            ph = PasswordHasher()

            # 验证密码
            if ph.verify(password_hash, password):
                # 检查是否需要重新哈希（参数变更时）
                if ph.check_needs_rehash(password_hash):
                    logger.info("检测到需要重新哈希的用户密码")
                    # 注意：实际重新哈希应在登录成功后执行
                return True
            return False
        except VerificationError:
            return False
        except ImportError:
            logger.error("argon2-cffi 未安装，无法验证 Argon2id 哈希")
            return False

    # 降级方案：PBKDF2-SHA256验证（向后兼容）
    try:
        salt, stored_hash = password_hash.split("$", 1)
        # 尝试新迭代次数（600,000）
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000)
        if hmac.compare_digest(dk.hex(), stored_hash):
            return True
        # 回退到旧迭代次数（100,000），验证成功后标记需要迁移
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
        return hmac.compare_digest(dk.hex(), stored_hash)
    except (ValueError, TypeError) as e:
        logger.warning(f"密码哈希格式异常: {e}")
        return False
    except Exception:
        logger.exception("密码验证过程中发生异常")
        return False


def _create_token(
    user_id: int,
    username: str,
    role: str,
    token_type: str | None,
    expire_hours: int,
    jti_bytes: int = 8,
) -> str:
    """Create a signed JWT token with standard claims.

    Args:
        user_id: User ID (stored as string in ``sub`` claim).
        username: Username (stored in ``username`` claim).
        role: User role string.
        token_type: Optional token type label (``"access"`` / ``"refresh"`` / ``None``).
        expire_hours: Token lifetime in hours.
        jti_bytes: Number of random bytes for the JTI (default 8, use 16 for refresh).
    """
    import secrets

    secret = _config.JWT_SECRET
    if not secret:
        raise ValueError("JWT_SECRET 未配置，无法生成 token")
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "jti": secrets.token_hex(jti_bytes),
        "iat": int(time.time()),
        "exp": int(time.time()) + expire_hours * 3600,
    }
    if token_type is not None:
        payload["type"] = token_type
    return jwt.encode(payload, secret, algorithm=_JWT_ALGORITHM)


def create_token(user_id: int, username: str, role: str) -> str:
    """生成 JWT token（v4.0: 增加 jti claim 支持吊销）"""
    return _create_token(user_id, username, role, token_type=None, expire_hours=_JWT_EXPIRE_HOURS)


def create_access_token(user_id: int, username: str, role: str) -> str:
    """P2-3: 生成短生命周期 access_token（默认 2 小时）"""
    expire_hours = getattr(_config, "JWT_ACCESS_EXPIRE_HOURS", 2)
    return _create_token(user_id, username, role, token_type="access", expire_hours=expire_hours)


async def create_refresh_token(user_id: int, username: str, role: str) -> str:
    """P2-3: 生成长生命周期 refresh_token（默认 7 天），存入 Redis 便于吊销"""
    expire_hours = getattr(_config, "JWT_REFRESH_EXPIRE_HOURS", 168)
    token = _create_token(
        user_id,
        username,
        role,
        token_type="refresh",
        expire_hours=expire_hours,
        jti_bytes=16,
    )

    # 存储 refresh_token JTI 到 Redis（用于主动吊销）
    if _denylist._use_redis:
        # Extract jti from the just-created token for Redis storage
        try:
            payload = jwt.decode(
                token,
                _config.JWT_SECRET,
                algorithms=[_JWT_ALGORITHM],
                options={"verify_exp": False},
            )
            jti = payload.get("jti")
        except (jwt.DecodeError, jwt.InvalidTokenError):
            jti = None
        if jti:
            try:
                refresh_prefix = getattr(
                    _config,
                    "REDIS_JWT_PREFIX",
                    "csai:jwt:blacklist:",
                ).replace("blacklist", "refresh")
                await asyncio.to_thread(
                    _denylist._redis.set,
                    f"{refresh_prefix}{jti}",
                    json.dumps({"user_id": user_id, "username": username}),
                    ex=expire_hours * 3600,
                )
            except _redis_mod.RedisError:
                pass

    return token


def refresh_access_token(refresh_token: str) -> dict[str, Any] | None:
    """P2-3: 用 refresh_token 换取新的 access_token"""
    payload = decode_token(refresh_token)
    if not payload:
        return None
    if payload.get("type") != "refresh":
        return None

    # 验证用户仍然有效
    db = get_db_session()
    try:
        user = (
            db.query(User)
            .filter(
                User.id == payload.get("sub"),
                User.is_active == 1,
            )
            .first()
        )
        if not user:
            return None

        new_access_token = create_access_token(user.id, user.username, user.role)
        return {
            "access_token": new_access_token,
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
        }
    except SQLAlchemyError:
        logger.exception("刷新 access_token 时数据库异常")
        return None
    finally:
        db.close()


async def revoke_user_tokens(user_id: int) -> int:
    """吊销指定用户的所有 refresh_token（密码修改时调用）
    v5.0: 添加 scan_iter count 限制和最大迭代次数，防止 Redis 阻塞
    v5.1: 使用 asyncio.to_thread 避免阻塞事件循环
    """
    if not _denylist._use_redis:
        return 0
    try:
        refresh_prefix = getattr(_config, "REDIS_JWT_PREFIX", "csai:jwt:blacklist:").replace(
            "blacklist", "refresh"
        )
        revoked = 0
        max_keys = 5000  # 最多扫描 5000 个 key，防止 Redis 阻塞
        scanned = 0

        def _scan_and_revoke():
            nonlocal revoked, scanned
            for key in _denylist._redis.scan_iter(f"{refresh_prefix}*", count=100):
                scanned += 1
                if scanned > max_keys:
                    logger.warning(f"revoke_user_tokens: 扫描超过 {max_keys} 个 key，提前终止")
                    break
                data = _denylist._redis.get(key)
                if data:
                    try:
                        info = json.loads(data)
                        if info.get("user_id") == user_id:
                            _denylist._redis.delete(key)
                            revoked += 1
                    except (json.JSONDecodeError, TypeError):
                        pass
            return revoked

        result = await asyncio.to_thread(_scan_and_revoke)
        if result:
            logger.info(f"已吊销用户 {user_id} 的 {result} 个 refresh_token")
        return result
    except _redis_mod.RedisError as e:
        logger.warning(f"吊销用户 refresh_token 失败: {e}")
        return 0


def decode_token(token: str) -> dict[str, Any] | None:
    """验证并解码 JWT token（v4.0: 支持 jti 吊销检查）"""
    secret = _config.JWT_SECRET
    if not secret:
        return None
    try:
        # PyJWT 验证签名、过期时间，算法白名单防止 alg:none 攻击
        payload = jwt.decode(token, secret, algorithms=[_JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, jwt.DecodeError):
        return None

    # v4.0: 检查 jti 是否在吊销黑名单中
    jti = payload.get("jti")
    if jti:
        # 本地 LRU 缓存快速拒绝
        if jti in _revoked_jtis:
            return None
        # Redis 黑名单同步检查
        # 注意：decode_token() 是同步函数，在异步上下文中调用会阻塞事件循环
        # 建议在异步路由中改用 decode_token_async() 代替
        if _denylist._use_redis:
            try:
                key = f"{_denylist._prefix}{jti}"
                if _denylist._redis.get(key):
                    return None
            except Exception:
                pass  # Redis 查询失败时放行（降级行为）
        elif jti in _denylist._memory_set:
            return None
    # PyJWT 要求 sub 为字符串，但下游代码期望 int，这里转回
    with contextlib.suppress(ValueError, TypeError, KeyError):
        payload["sub"] = int(payload["sub"])
    return payload


async def decode_token_async(token: str) -> dict[str, Any] | None:
    """异步版本：验证并解码 JWT token，支持 Redis 黑名单检查"""
    secret = _config.JWT_SECRET
    if not secret:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=[_JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, jwt.DecodeError):
        return None

    # 检查 jti 是否在吊销黑名单中
    jti = payload.get("jti")
    if jti and await _denylist.contains(jti):
        return None
    with contextlib.suppress(ValueError, TypeError, KeyError):
        payload["sub"] = int(payload["sub"])
    return payload


def get_current_user(token: str) -> User | None:
    """从 JWT token 获取当前用户"""
    payload = decode_token(token)
    if not payload:
        return None
    db = get_db_session()
    try:
        user = db.query(User).filter(User.id == payload.get("sub")).first()
        if user and user.is_active:
            return user
        return None
    finally:
        db.close()


def get_user_id_from_request(request) -> int | None:
    """从请求中提取当前用户 ID（用于工具调用权限校验）"""
    auth_header = request.headers.get("Authorization", "")
    jwt_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not jwt_token:
        return None
    payload = decode_token(jwt_token)
    if payload:
        return payload.get("sub")
    return None


async def revoke_token(token: str) -> bool:
    """吊销 JWT token（将 jti 加入黑名单）"""
    payload = decode_token(token)
    if not payload:
        return False
    jti = payload.get("jti")
    if jti:
        exp = payload.get("exp", int(time.time()) + _JWT_EXPIRE_HOURS * 3600)
        ttl = max(int(exp - time.time()), 1)
        await _denylist.add(jti, ttl)
        _revoked_jtis.add(jti)
        # v6.2: 容量限制，超出时淘汰最早的 10%
        if len(_revoked_jtis) > _MAX_REVOKED_JTIS:
            _evict_count = _MAX_REVOKED_JTIS // 10
            for _ in range(_evict_count):
                _revoked_jtis.pop()
            logger.warning(
                f"已吊销 JTI 缓存达到上限 {_MAX_REVOKED_JTIS}，淘汰 {_evict_count} 条旧记录"
            )
        logger.info(f"Token 已吊销: jti={jti}")
        return True
    return False


def register_user(username: str, password: str, display_name: str = "") -> dict[str, Any]:
    """注册用户"""
    db = get_db_session()
    try:
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            return {"success": False, "error": "用户名已存在"}

        user = User(
            username=username,
            password_hash=hash_password(password),
            role="customer",
            display_name=display_name or username,
            created_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"用户注册成功: {username} (id={user.id})")
        return {"success": True, "user_id": user.id, "username": username}
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"用户注册失败: {e}", exc_info=True)
        return {"success": False, "error": "注册失败，请稍后重试"}
    finally:
        db.close()


async def authenticate_user(username: str, password: str) -> dict[str, Any] | None:
    """验证用户并返回 token"""
    db = get_db_session()
    try:
        user = db.query(User).filter(User.username == username, User.is_active == 1).first()
        if not user or not verify_password(password, user.password_hash):
            return None

        # 更新最后登录时间
        user.last_login_at = datetime.now(timezone.utc)
        db.commit()

        token = create_token(user.id, user.username, user.role)
        refresh_token = await create_refresh_token(user.id, user.username, user.role)
        return {
            "token": token,
            "refresh_token": refresh_token,
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
            "display_name": user.display_name,
        }
    finally:
        db.close()


def init_default_admin():
    """初始化默认管理员账号（v4.0: 随机密码，不写入日志）"""
    db = get_db_session()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            admin_password = os.getenv("ADMIN_PASSWORD")
            if not admin_password:
                logger.error(
                    "[auth] 致命错误: 未设置 ADMIN_PASSWORD 环境变量。必须显式设置管理员密码。"
                )
                raise ValueError(
                    "ADMIN_PASSWORD environment variable must be set to initialize the admin account."
                )

            admin = User(
                username="admin",
                password_hash=hash_password(admin_password),
                role="admin",
                display_name="系统管理员",
                created_at=datetime.now(timezone.utc),
                force_password_change=1,  # 标记需要修改密码
            )
            db.add(admin)
            db.commit()
            # v4.4 安全加固: 不写入密码文件，不输出密码或哈希到日志
            logger.info("[auth] 默认管理员已创建（密码来自 ADMIN_PASSWORD 环境变量）")
        else:
            logger.info("管理员账号已存在，跳过初始化")
    finally:
        db.close()
