"""认证模块（v4.4 — PyJWT + PBKDF2-SHA256）"""

from .router import router as auth_router
from .service import create_token, decode_token, hash_password, verify_password

__all__ = ["auth_router", "create_token", "decode_token", "hash_password", "verify_password"]
