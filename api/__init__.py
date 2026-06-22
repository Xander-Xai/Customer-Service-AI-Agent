"""FastAPI 服务层包。

避免在包导入阶段触发 `app_factory` 的数据库/容器初始化副作用。
需要应用实例时请显式导入 `api.app_factory` 或 `api.app:create_app`。
"""

__all__ = ["app", "app_factory", "middleware", "routes", "utils"]
