"""
配置文件（二次开发版 v3.0）
新增：结构化日志、CORS配置、ERP模式、API认证、监控指标
"""
import os
from dotenv import load_dotenv

load_dotenv(override=True)

# ===== OpenAI 兼容 API 配置 =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "Qwen/Qwen3-8B")

# ===== HTTP 请求配置 =====
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
HTTP_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "MultiAgentCustomerService/3.0.0"
}

# ===== 路由配置 =====
ROUTING_COMPLEXITY_THRESHOLD = int(os.getenv("ROUTING_COMPLEXITY_THRESHOLD", "50"))

# ===== 缓存配置 =====
CACHE_L1_MAX = int(os.getenv("CACHE_L1_MAX", "500"))
CACHE_L2_MAX = int(os.getenv("CACHE_L2_MAX", "2000"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))
CACHE_SEMANTIC_THRESHOLD_SHORT = float(os.getenv("CACHE_SEMANTIC_THRESHOLD_SHORT", "0.7"))
CACHE_SEMANTIC_THRESHOLD_LONG = float(os.getenv("CACHE_SEMANTIC_THRESHOLD_LONG", "0.5"))

# ===== 会话配置 =====
SESSION_WINDOW_SIZE = int(os.getenv("SESSION_WINDOW_SIZE", "10"))
SESSION_STORAGE_BACKEND = os.getenv("SESSION_STORAGE_BACKEND", "memory")
SESSION_MAX_TOKENS = int(os.getenv("SESSION_MAX_TOKENS", "4000"))
SESSION_SUMMARY_MAX_CHARS = int(os.getenv("SESSION_SUMMARY_MAX_CHARS", "500"))

# ===== 漂移检测配置 =====
DRIFT_TOPIC_JACCARD_THRESHOLD = float(os.getenv("DRIFT_TOPIC_JACCARD_THRESHOLD", "0.15"))
DRIFT_REPETITION_THRESHOLD = float(os.getenv("DRIFT_REPETITION_THRESHOLD", "0.8"))
DRIFT_ESCALATION_THRESHOLD = int(os.getenv("DRIFT_ESCALATION_THRESHOLD", "5"))

# ===== 连接池配置（httpx） =====
HTTPX_MAX_CONNECTIONS = int(os.getenv("HTTPX_MAX_CONNECTIONS", "100"))
HTTPX_KEEPALIVE_CONNECTIONS = int(os.getenv("HTTPX_KEEPALIVE_CONNECTIONS", "20"))

# ===== 响应时长 SLA 配置（v3.1 补充） =====
RESPONSE_TIME_TARGET_MIN = float(os.getenv("RESPONSE_TIME_TARGET_MIN", "5.0"))
RESPONSE_TIME_TARGET_MAX = float(os.getenv("RESPONSE_TIME_TARGET_MAX", "20.0"))

# ===== SLA 告警配置（v3.2 新增）=====
SLA_ALERT_WINDOW = int(os.getenv("SLA_ALERT_WINDOW", "50"))           # 滑动窗口大小（最近 N 次请求）
SLA_ALERT_THRESHOLD = float(os.getenv("SLA_ALERT_THRESHOLD", "30.0")) # 违约率阈值（%），超过则告警
SLA_ALERT_COOLDOWN = int(os.getenv("SLA_ALERT_COOLDOWN", "300"))      # 同类告警冷却时间（秒）

# ===== 模型熔断器配置（v3.2 新增）=====
CIRCUIT_BREAKER_FAIL_THRESHOLD = int(os.getenv("CIRCUIT_BREAKER_FAIL_THRESHOLD", "5"))  # 连续失败次数触发熔断
CIRCUIT_BREAKER_RECOVERY_TIME = int(os.getenv("CIRCUIT_BREAKER_RECOVERY_TIME", "60"))   # 熔断恢复时间（秒）
LLM_ROUTER_TIMEOUT = float(os.getenv("LLM_ROUTER_TIMEOUT", "8.0"))   # 路由 LLM 调用超时（秒）

# ===== 重试配置 =====
RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

# ===== 系统配置 =====
VERSION = "3.0.0"


# ===== 日志配置 =====
LOG_CONFIG = {
    "level": os.getenv("LOG_LEVEL", "INFO"),
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
}

# ===== CORS 配置（v3.0 新增） =====
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

# ===== API 认证配置（v3.0 新增） =====
API_KEY_ENABLED = os.getenv("API_KEY_ENABLED", "false").lower() == "true"
API_KEY = os.getenv("API_KEY", "")

# ===== ERP 模式配置（v3.0 新增） =====
ERP_MODE = os.getenv("ERP_MODE", "mock")  # mock | real
ERP_BASE_URL = os.getenv("ERP_BASE_URL", "")
ERP_APP_ID = os.getenv("ERP_APP_ID", "")
ERP_APP_SECRET = os.getenv("ERP_APP_SECRET", "")
ERP_DB_ID = os.getenv("ERP_DB_ID", "")

# ===== Redis 配置（v3.0 新增） =====
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")