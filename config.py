"""
配置文件（v3.9 — 生产就绪版）
新增：Prometheus 指标、增强健康检查、CORS 环境变量、日志轮转
"""
import os
from dotenv import load_dotenv

load_dotenv(override=True)


def _int_env(key: str, default: int) -> int:
    """v3.8 fix: safe int env conversion (prevents ValueError on invalid input)"""
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _float_env(key: str, default: float) -> float:
    """v3.8 fix: safe float env conversion"""
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


# ===== OpenAI 兼容 API 配置 =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "Qwen/Qwen3-8B")

# ===== HTTP 请求配置 =====
HTTP_TIMEOUT = _int_env("HTTP_TIMEOUT", 30)
HTTP_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "MultiAgentCustomerService/3.0.0"
}

# ===== 路由配置 =====
ROUTING_COMPLEXITY_THRESHOLD = _int_env("ROUTING_COMPLEXITY_THRESHOLD", 50)

# ===== 缓存配置 =====
CACHE_L1_MAX = _int_env("CACHE_L1_MAX", 500)
CACHE_L2_MAX = _int_env("CACHE_L2_MAX", 2000)
CACHE_TTL = _int_env("CACHE_TTL", 3600)
CACHE_SEMANTIC_THRESHOLD_SHORT = _float_env("CACHE_SEMANTIC_THRESHOLD_SHORT", 0.7)
CACHE_SEMANTIC_THRESHOLD_LONG = _float_env("CACHE_SEMANTIC_THRESHOLD_LONG", 0.5)

# ===== 会话配置 =====
SESSION_WINDOW_SIZE = _int_env("SESSION_WINDOW_SIZE", 10)
SESSION_STORAGE_BACKEND = os.getenv("SESSION_STORAGE_BACKEND", "memory")
SESSION_MAX_TOKENS = _int_env("SESSION_MAX_TOKENS", 4000)
SESSION_SUMMARY_MAX_CHARS = _int_env("SESSION_SUMMARY_MAX_CHARS", 500)

# ===== 漂移检测配置 =====
DRIFT_TOPIC_JACCARD_THRESHOLD = _float_env("DRIFT_TOPIC_JACCARD_THRESHOLD", 0.15)
DRIFT_REPETITION_THRESHOLD = _float_env("DRIFT_REPETITION_THRESHOLD", 0.8)
DRIFT_ESCALATION_THRESHOLD = _int_env("DRIFT_ESCALATION_THRESHOLD", 5)

# ===== 连接池配置（httpx） =====
HTTPX_MAX_CONNECTIONS = _int_env("HTTPX_MAX_CONNECTIONS", 100)
HTTPX_KEEPALIVE_CONNECTIONS = _int_env("HTTPX_KEEPALIVE_CONNECTIONS", 20)

# ===== 响应时长 SLA 配置（v3.1 补充） =====
RESPONSE_TIME_TARGET_MIN = _float_env("RESPONSE_TIME_TARGET_MIN", 5.0)
RESPONSE_TIME_TARGET_MAX = _float_env("RESPONSE_TIME_TARGET_MAX", 20.0)

# ===== SLA 告警配置（v3.2 新增）=====
SLA_ALERT_WINDOW = _int_env("SLA_ALERT_WINDOW", 50)
SLA_ALERT_THRESHOLD = _float_env("SLA_ALERT_THRESHOLD", 30.0)
SLA_ALERT_COOLDOWN = _int_env("SLA_ALERT_COOLDOWN", 300)

# ===== 模型熔断器配置（v3.2 新增）=====
CIRCUIT_BREAKER_FAIL_THRESHOLD = _int_env("CIRCUIT_BREAKER_FAIL_THRESHOLD", 5)
CIRCUIT_BREAKER_RECOVERY_TIME = _int_env("CIRCUIT_BREAKER_RECOVERY_TIME", 60)
LLM_ROUTER_TIMEOUT = _float_env("LLM_ROUTER_TIMEOUT", 8.0)

# ===== 重试配置 =====
RETRY_MAX_ATTEMPTS = _int_env("RETRY_MAX_ATTEMPTS", 3)
RETRY_BASE_DELAY = _float_env("RETRY_BASE_DELAY", 1.0)

# ===== 系统配置 =====
VERSION = "3.8.0"

# ===== v3.4: 安全配置 ======
MAX_QUERY_LENGTH = _int_env("MAX_QUERY_LENGTH", 2000)
MAX_SESSIONS = _int_env("MAX_SESSIONS", 10000)
SESSION_IDLE_TTL = _int_env("SESSION_IDLE_TTL", 3600)

# ===== v3.7: 安全加固配置 =====
# 监控端点管理 Token（/api/metrics, /api/kpi 等敏感端点需要此 Token）
MONITORING_ADMIN_TOKEN = os.getenv("MONITORING_ADMIN_TOKEN", "")
# WebSocket 连接限制
WS_MAX_CONNECTIONS_PER_IP = _int_env("WS_MAX_CONNECTIONS_PER_IP", 5)
WS_MESSAGE_RATE_LIMIT = _int_env("WS_MESSAGE_RATE_LIMIT", 10)
WS_IDLE_TIMEOUT = _int_env("WS_IDLE_TIMEOUT", 300)
# 会话令牌签名密钥（用于防会话劫持）
SESSION_TOKEN_SECRET = os.getenv("SESSION_TOKEN_SECRET", "")
# TLS 配置
TLS_CERT_FILE = os.getenv("TLS_CERT_FILE", "")    # TLS 证书文件路径
TLS_KEY_FILE = os.getenv("TLS_KEY_FILE", "")      # TLS 私钥文件路径


# ===== 日志配置 =====
LOG_CONFIG = {
    "level": os.getenv("LOG_LEVEL", "INFO"),
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
}

# ===== CORS 配置（v3.0 新增） =====
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

# ===== API 认证配置（v3.0 新增） =====
API_KEY_ENABLED = os.getenv("API_KEY_ENABLED", "true").lower() == "true"
API_KEY = os.getenv("API_KEY", "")
if API_KEY_ENABLED and not API_KEY:
    raise ValueError("API_KEY_ENABLED=true requires a non-empty API_KEY. Set API_KEY in .env or disable authentication with API_KEY_ENABLED=false")

# ===== ERP 模式配置（v3.0 新增） =====
ERP_MODE = os.getenv("ERP_MODE", "mock")  # mock | real
ERP_BASE_URL = os.getenv("ERP_BASE_URL", "")
ERP_APP_ID = os.getenv("ERP_APP_ID", "")
ERP_APP_SECRET = os.getenv("ERP_APP_SECRET", "")
ERP_DB_ID = os.getenv("ERP_DB_ID", "")

# ===== Redis 配置（v3.0 新增） =====
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# ===== v3.5: RAG 配置 =====
RAG_PERSIST_DIRECTORY = os.getenv("RAG_PERSIST_DIRECTORY", "")  # 空则内存模式
RAG_N_RESULTS = _int_env("RAG_N_RESULTS", 3)

# ===== v3.5: ReAct 配置 =====
REACT_MAX_ITERATIONS = _int_env("REACT_MAX_ITERATIONS", 5)
REACT_COMPLEXITY_THRESHOLD = _int_env("REACT_COMPLEXITY_THRESHOLD", 60)

# ===== v3.5: 工具调用配置 =====
TOOL_MAX_ROUNDS = _int_env("TOOL_MAX_ROUNDS", 3)