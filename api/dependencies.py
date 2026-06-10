"""
FastAPI 依赖注入辅助（v5.1）
提供 get_container / get_service 依赖函数，替代模块级全局变量访问。

使用方式：
    from api.dependencies import get_container, get_metrics

    @router.get("/api/some-endpoint")
    async def endpoint(container: ServiceContainer = Depends(get_container)):
        ...
"""

from typing import Any

from fastapi import Request

from core.container import ServiceContainer


def get_container(request: Request) -> ServiceContainer:
    """从 app.state 获取 ServiceContainer（由 app_factory 注入）"""
    container: ServiceContainer | None = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("ServiceContainer 未初始化，请检查 app_factory 启动流程")
    return container


def get_metrics(request: Request) -> Any:
    """从 app.state 获取 MetricsCollector"""
    container = get_container(request)
    return container.metrics


def get_token_tracker(request: Request) -> Any:
    """从全局单例获取 TokenTracker"""
    from core.token_tracker import get_token_tracker as _get

    tracker = _get()
    if tracker is None:
        raise RuntimeError("TokenTracker 未初始化")
    return tracker
