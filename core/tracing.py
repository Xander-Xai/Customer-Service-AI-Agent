"""
OpenTelemetry 分布式追踪集成（P2-1）
可选模块，通过环境变量 OPENTELEMETRY_ENABLED=true 启用。

Usage:
    # 启用 OpenTelemetry
    export OPENTELEMETRY_ENABLED=true
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317

    # 在 app_factory.py 中调用:
    from core.tracing import setup_tracing
    setup_tracing(app)
"""
import os
from logger import get_logger

logger = get_logger("core.tracing")

_TRACING_INITIALIZED = False


def setup_tracing(app=None) -> bool:
    """
    初始化 OpenTelemetry 分布式追踪。
    返回 True 表示成功启用，False 表示跳过。

    Args:
        app: FastAPI 应用实例（可选，传入时自动 instrument FastAPI）
    """
    global _TRACING_INITIALIZED

    if _TRACING_INITIALIZED:
        return True

    if os.getenv("OPENTELEMETRY_ENABLED", "").lower() != "true":
        logger.info("OpenTelemetry 未启用（设置 OPENTELEMETRY_ENABLED=true 启用）")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        # 创建 TracerProvider
        service_name = os.getenv("OTEL_SERVICE_NAME", "csai-customer-service")
        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)

        # 配置 OTLP 导出器
        otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))

        # 设置全局 TracerProvider
        trace.set_tracer_provider(provider)

        # 自动 instrument FastAPI
        if app:
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
                FastAPIInstrumentor.instrument_app(app)
                logger.info("FastAPI 自动追踪已启用")
            except ImportError:
                logger.warning("opentelemetry-instrumentation-fastapi 未安装，跳过 FastAPI 追踪")

        # 自动 instrument httpx（LLM 调用追踪）
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
            HTTPXClientInstrumentor().instrument()
            logger.info("httpx 自动追踪已启用")
        except ImportError:
            logger.warning("opentelemetry-instrumentation-httpx 未安装，跳过 httpx 追踪")

        # 自动 instrument SQLAlchemy（数据库查询追踪）
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            SQLAlchemyInstrumentor().instrument()
            logger.info("SQLAlchemy 自动追踪已启用")
        except ImportError:
            logger.warning("opentelemetry-instrumentation-sqlalchemy 未安装，跳过 DB 追踪")

        _TRACING_INITIALIZED = True
        logger.info(f"✅ OpenTelemetry 分布式追踪已启用 (service={service_name}, endpoint={otlp_endpoint})")
        return True

    except ImportError as e:
        logger.warning(f"OpenTelemetry SDK 未安装: {e}")
        return False
    except Exception as e:
        logger.warning(f"OpenTelemetry 初始化失败: {e}")
        return False


def get_tracer(name: str = "csai"):
    """获取 Tracer 实例"""
    if not _TRACING_INITIALIZED:
        return None
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except Exception as e:
        logger.debug(f"获取 Tracer 失败: {e}")
        return None
