"""认证模块（PyJWT + Argon2id 主路径，PBKDF2-SHA256 向后兼容回退）"""

from .router import router as auth_router
from .service import create_token, decode_token, hash_password, verify_password

__all__ = ["auth_router", "create_token", "decode_token", "hash_password", "verify_password"]
