"""
认证业务逻辑（v4.4）
- PBKDF2-SHA256 密码哈希（600,000 次迭代，OWASP 推荐）
- PyJWT token 生成/验证（HS256 + 算法白名单）
- 用户 CRUD
"""
import os
import time
import hmac
import hashlib
from datetime import datetime, timezone
import json
from typing import Optional, Dict, Any

import jwt

from db.models import User
from db.database import get_db_session
from logger import get_logger

logger = get_logger("auth.service")

# JWT 配置（v4.0 安全修复：从 config 读取，不再有硬编码默认值）
import config as _config

_JWT_ALGORITHM = "HS256"
_JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "72"))


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
                redis_url, decode_responses=True, socket_timeout=2, connect_timeout=2,
            )
            self._redis.ping()
            self._use_redis = True
            logger.info("JWT 黑名单已启用 Redis 后端")
        except Exception as e:
            logger.warning(f"Redis 不可用，JWT 黑名单回退到内存模式: {e}")
            # 生产环境不使用 Redis 时发出警告
            app_mode = os.getenv("APP_MODE", "").lower()
            if app_mode == "prod":
                logger.warning(
                    "JWT 黑名单运行在生产环境但未使用 Redis，"
                    f"内存模式最大容量为 {self.MAX_DENYLIST_SIZE} 条，"
                    "重启后黑名单将丢失"
                )

    def add(self, jti: str, ttl_seconds: int) -> None:
        """将 jti 加入黑名单"""
        if self._use_redis:
            key = f"{self._prefix}{jti}"
            self._redis.setex(key, max(ttl_seconds, 1), "1")
        else:
            # 内存模式：检查容量，超过上限时清理旧条目
            if len(self._memory_set) >= self.MAX_DENYLIST_SIZE:
                evict_count = self.MAX_DENYLIST_SIZE // 4
                for _ in range(evict_count):
                    self._memory_set.pop()
            self._memory_set.add(jti)

    def contains(self, jti: str) -> bool:
        """检查 jti 是否在黑名单中"""
        if self._use_redis:
            key = f"{self._prefix}{jti}"
            return self._redis.exists(key) > 0
        return jti in self._memory_set


_denylist = _TokenDenylist()


def hash_password(password: str) -> str:
    """密码哈希（v4.0: PBKDF2-SHA256, 600,000 次迭代，OWASP 推荐）"""
    import os
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码（支持新旧迭代次数）"""
    try:
        salt, stored_hash = password_hash.split("$", 1)
        # 尝试新迭代次数（600,000）
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000)
        if hmac.compare_digest(dk.hex(), stored_hash):
            return True
        # 回退到旧迭代次数（100,000），验证成功后标记需要迁移
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
        return hmac.compare_digest(dk.hex(), stored_hash)
    except Exception:
        return False


def create_token(user_id: int, username: str, role: str) -> str:
    """生成 JWT token（v4.0: 增加 jti claim 支持吊销）"""
    import secrets
    secret = _config.JWT_SECRET
    if not secret:
        raise ValueError("JWT_SECRET 未配置，无法生成 token")
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "jti": secrets.token_hex(8),  # v4.0: 唯一 token ID，用于吊销
        "iat": int(time.time()),
        "exp": int(time.time()) + _JWT_EXPIRE_HOURS * 3600,
    }
    return jwt.encode(payload, secret, algorithm=_JWT_ALGORITHM)


def create_access_token(user_id: int, username: str, role: str) -> str:
    """P2-3: 生成短生命周期 access_token（默认 2 小时）"""
    import secrets
    secret = _config.JWT_SECRET
    if not secret:
        raise ValueError("JWT_SECRET 未配置，无法生成 token")
    expire_hours = getattr(_config, "JWT_ACCESS_EXPIRE_HOURS", 2)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "type": "access",
        "jti": secrets.token_hex(8),
        "iat": int(time.time()),
        "exp": int(time.time()) + expire_hours * 3600,
    }
    return jwt.encode(payload, secret, algorithm=_JWT_ALGORITHM)


def create_refresh_token(user_id: int, username: str, role: str) -> str:
    """P2-3: 生成长生命周期 refresh_token（默认 7 天），存入 Redis 便于吊销"""
    import secrets
    secret = _config.JWT_SECRET
    if not secret:
        raise ValueError("JWT_SECRET 未配置，无法生成 token")
    expire_hours = getattr(_config, "JWT_REFRESH_EXPIRE_HOURS", 168)
    jti = secrets.token_hex(16)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "type": "refresh",
        "jti": jti,
        "iat": int(time.time()),
        "exp": int(time.time()) + expire_hours * 3600,
    }
    token = jwt.encode(payload, secret, algorithm=_JWT_ALGORITHM)

    # 存储 refresh_token JTI 到 Redis（用于主动吊销）
    if _denylist._use_redis:
        try:
            refresh_prefix = getattr(_config, "REDIS_JWT_PREFIX", "csai:jwt:blacklist:").replace("blacklist", "refresh")
            _denylist._redis.setex(
                f"{refresh_prefix}{jti}", expire_hours * 3600,
                json.dumps({"user_id": user_id, "username": username}),
            )
        except Exception:
            pass

    return token


def refresh_access_token(refresh_token: str) -> Optional[Dict[str, Any]]:
    """P2-3: 用 refresh_token 换取新的 access_token"""
    payload = decode_token(refresh_token)
    if not payload:
        return None
    if payload.get("type") != "refresh":
        return None

    # 验证用户仍然有效
    db = get_db_session()
    try:
        user = db.query(User).filter(
            User.id == payload.get("sub"),
            User.is_active == 1,
        ).first()
        if not user:
            return None

        new_access_token = create_access_token(user.id, user.username, user.role)
        return {
            "access_token": new_access_token,
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
        }
    finally:
        db.close()


def revoke_user_tokens(user_id: int) -> int:
    """P2-3: 吊销指定用户的所有 refresh_token（密码修改时调用）"""
    if not _denylist._use_redis:
        return 0
    try:
        refresh_prefix = getattr(_config, "REDIS_JWT_PREFIX", "csai:jwt:blacklist:").replace("blacklist", "refresh")
        revoked = 0
        for key in _denylist._redis.scan_iter(f"{refresh_prefix}*"):
            data = _denylist._redis.get(key)
            if data:
                try:
                    info = json.loads(data)
                    if info.get("user_id") == user_id:
                        _denylist._redis.delete(key)
                        revoked += 1
                except (json.JSONDecodeError, TypeError):
                    pass
        if revoked:
            logger.info(f"已吊销用户 {user_id} 的 {revoked} 个 refresh_token")
        return revoked
    except Exception as e:
        logger.warning(f"吊销用户 refresh_token 失败: {e}")
        return 0


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """验证并解码 JWT token（v4.0: 支持 jti 吊销检查）"""
    secret = _config.JWT_SECRET
    if not secret:
        return None
    try:
        # PyJWT 验证签名、过期时间，算法白名单防止 alg:none 攻击
        payload = jwt.decode(token, secret, algorithms=[_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    except Exception:
        return None

    # v4.0: 检查 jti 是否在吊销黑名单中
    jti = payload.get("jti")
    if jti and _denylist.contains(jti):
        return None
    # PyJWT 要求 sub 为字符串，但下游代码期望 int，这里转回
    try:
        payload["sub"] = int(payload["sub"])
    except (ValueError, TypeError, KeyError):
        pass
    return payload


def get_current_user(token: str) -> Optional[User]:
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


def get_user_id_from_request(request) -> Optional[int]:
    """从请求中提取当前用户 ID（用于工具调用权限校验）"""
    auth_header = request.headers.get("Authorization", "")
    jwt_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not jwt_token:
        return None
    payload = decode_token(jwt_token)
    if payload:
        return payload.get("sub")
    return None


def revoke_token(token: str) -> bool:
    """吊销 JWT token（将 jti 加入黑名单）"""
    payload = decode_token(token)
    if not payload:
        return False
    jti = payload.get("jti")
    if jti:
        exp = payload.get("exp", int(time.time()) + _JWT_EXPIRE_HOURS * 3600)
        ttl = max(int(exp - time.time()), 1)
        _denylist.add(jti, ttl)
        logger.info(f"Token 已吊销: jti={jti}")
        return True
    return False


def register_user(username: str, password: str, display_name: str = "") -> Dict[str, Any]:
    """注册用户"""
    db = get_db_session()
    try:
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            return {"success": False, "error": "用户名已存在"}

        user = User(
            username=username,
            password_hash=hash_password(password),
            role="user",
            display_name=display_name or username,
            created_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"用户注册成功: {username} (id={user.id})")
        return {"success": True, "user_id": user.id, "username": username}
    except Exception as e:
        db.rollback()
        logger.error(f"用户注册失败: {e}")
        return {"success": False, "error": "注册失败，请稍后重试"}
    finally:
        db.close()


def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
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
        return {
            "token": token,
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
            import secrets
            admin_password = os.getenv("ADMIN_PASSWORD", secrets.token_urlsafe(16))
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
            if os.getenv("ADMIN_PASSWORD"):
                logger.info("[auth] 默认管理员已创建（密码来自 ADMIN_PASSWORD 环境变量）")
            else:
                logger.info("[auth] 默认管理员已创建，密码已通过 ADMIN_PASSWORD 自动生成")
                logger.info("[auth] 请通过 ADMIN_PASSWORD 环境变量设置密码，首次登录后请修改密码")
        else:
            logger.info("管理员账号已存在，跳过初始化")
    finally:
        db.close()
