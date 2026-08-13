"""
Trace ID 全链路传播中间件（v6.1 — 证据缺口修复）

为每个 HTTP 请求生成/传递 X-Trace-ID，并传播到日志和下游服务。
从 api/middleware/__init__.py 提取为独立模块，提高可测试性和可导入性。

符合 Starlette BaseHTTPMiddleware 标准，可配合 app.add_middleware() 注册。

用法:
    app.add_middleware(TraceMiddleware)
"""

import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from core.logger import set_trace_id

TRACE_ID_HEADER = "X-Trace-ID"


class TraceMiddleware(BaseHTTPMiddleware):
    """为每个请求生成/传递 Trace ID，并传播到日志和下游。"""

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get(TRACE_ID_HEADER, uuid.uuid4().hex)
        set_trace_id(trace_id)
        request.state.trace_id = trace_id

        response: Response = await call_next(request)
        response.headers[TRACE_ID_HEADER] = trace_id

        return response
