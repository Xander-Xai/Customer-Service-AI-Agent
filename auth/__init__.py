"""认证模块（v4.0 — JWT + bcrypt）"""
from .router import router as auth_router
from .service import hash_password, verify_password, create_token, decode_token
