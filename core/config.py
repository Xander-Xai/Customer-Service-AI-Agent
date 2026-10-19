"""
配置文件（v4.1 — 接入 DeepSeek + 依赖注入 + 流式输出版）
支持 DEV / PROD / TEST 三套配置，通过 .env 文件切换
"""

import json
import logging
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(override=True)


# ===== v5.1: 自定义配置异常 =====
class ConfigurationError(Exception):
    """生产环境配置缺失或无效时抛出，替代 SystemExit。"""

    pass


# ===== v4.1: 环境标识（启动时输出）=====
_DEV_MODE = os.getenv("DEV_MODE", "").lower() == "true"
_ENV_LABEL = "DEV" if _DEV_MODE else "PROD"
logging.getLogger("config").info(f"Environment: {_ENV_LABEL} (DEV_MODE={_DEV_MODE})")


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


# ===== OpenAI 兼容 API 配置（v4.1: 默认硅基流动）=====
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "siliconflow")  # siliconflow | deepseek | openai | custom
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "Qwen/Qwen3-8B")  # v6.0: 从 Qwen2.5-7B 升级
LLM_MAX_TOKENS = _int_env("LLM_MAX_TOKENS", 4096)


# ===== LLM API Key 可用性：单一权威判定（issue #51）=====
#
# 为什么必须只有一份：运行时会用「key 是否可用」决定**到底跑哪个 LLM 实现**
# （不可用就退到 RuleBasedLLM），而 /api/health 用它告诉运维「key 是不是好的」。
# 这两处一旦各自实现，就会出现最糟的组合：health 报 key_valid=true，
# 同时进程正在用规则引擎模板回答所有请求 —— 而 /api/health 既是 README 的验证
# 入口，也是 compose 给 app 容器定的 healthcheck，于是这个部署会通过自己的健康
# 门禁。健康面与运行面必须读同一个判定。
#
# 本函数是**纯函数**：只做字符串检查，不发任何网络请求。/api/health 是无鉴权
# 端点，绝不能因为"确认一下 key"就去打付费 provider（那既是延迟来源，也是把
# 无鉴权端点变成可被反复触发的计费入口）。key 到底能不能用，由启动期的
# ServiceContainer._check_llm_health 用真实调用确认，它的结论不进这里。

#: 占位/示例 key 的前缀黑名单（小写比较）。
#:
#: 这是历史两套实现的历史并集，不是新造的第三套：
#:   core/container.py 曾用 ("your_", "test-", "mock-", "sk-placeholder", "sk-xxx", "sk-your")
#:   api/routes/monitoring.py 曾用 ("sk-placeholder", "your-", "sk-xxx", "sk-your", "sk-test-placeholder")
#: 两者在 "your_" vs "your-"（下划线 vs 连字符）、test-/mock-/sk-test-placeholder
#: 的归属上就不一致，因此 mock- 开头的 45 字符 key 会被 health 判成可用、
#: 被运行时判成不可用。并集保证任一路径原来认定的占位 key 仍然被认定。
LLM_PLACEHOLDER_PREFIXES: tuple[str, ...] = (
    "your_",
    "your-",
    "test-",
    "mock-",
    "sk-placeholder",
    "sk-xxx",
    "sk-your",
    "sk-test-placeholder",
)

#: 真实 key 的最小长度。SiliconFlow / OpenAI / DeepSeek 的 key 都在 40 字符以上；
#: 低于该长度的字符串几乎一定是占位符或测试残留。运行时原本就有这条规则，
#: health 路径缺失 —— 这正是 issue #51 报告的核心症状。
LLM_API_KEY_MIN_LENGTH = 40

#: evaluate_llm_api_key() 的 reason 取值（稳定字符串，便于日志与测试断言）。
LLM_KEY_REASON_OK = "ok"
LLM_KEY_REASON_EMPTY = "empty"
LLM_KEY_REASON_PLACEHOLDER = "placeholder_prefix"
LLM_KEY_REASON_TOO_SHORT = "too_short"


@dataclass(frozen=True)
class LlmKeyStatus:
    """``OPENAI_API_KEY`` 的判定结果（不可变）。

    :param configured: 是否设置了非空值（"有没有配"）
    :param usable: 是否可用于真实 provider 调用（"配了且能不能用"）
    :param reason: ``usable`` 为 False 时的原因，取值见 ``LLM_KEY_REASON_*``
    :param length: 实际长度（不泄漏 key 内容，便于排查"我明明配了"）
    """

    configured: bool
    usable: bool
    reason: str
    length: int


def evaluate_llm_api_key(api_key: str | None = None) -> LlmKeyStatus:
    """判定 LLM API Key 是否可用于真实 provider 调用（纯函数，无 IO）。

    这是**唯一**权威实现：`core/container.py` 的运行时选择与
    `api/routes/monitoring.py` 的健康上报都必须调用它，不得各自内联规则。

    规则（与历史运行时行为一致，并把缺失的长度规则补齐到所有调用方）：

    1. 空 / 缺失 → ``configured=False, usable=False``（reason=empty）
    2. 以任一 :data:`LLM_PLACEHOLDER_PREFIXES` 开头（大小写不敏感）→ 不可用
    3. 长度 < :data:`LLM_API_KEY_MIN_LENGTH` → 不可用（reason=too_short）
    4. 否则可用

    参数默认读 :data:`OPENAI_API_KEY`，因此健康面与运行时读的是同一个来源，
    避免"一个读 os.environ、一个读 core.config"这种时序上的分叉。

    注意：本函数**不验证 key 是否真的被 provider 接受**。它只排除明显不可用的
    值；真实可用性由启动期的一次实际调用确认。
    """
    if api_key is None:
        api_key = OPENAI_API_KEY
    key = api_key.strip()

    if not key:
        return LlmKeyStatus(configured=False, usable=False, reason=LLM_KEY_REASON_EMPTY, length=0)

    lowered = key.lower()
    if any(lowered.startswith(prefix) for prefix in LLM_PLACEHOLDER_PREFIXES):
        return LlmKeyStatus(
            configured=True,
            usable=False,
            reason=LLM_KEY_REASON_PLACEHOLDER,
            length=len(key),
        )

    if len(key) < LLM_API_KEY_MIN_LENGTH:
        return LlmKeyStatus(
            configured=True,
            usable=False,
            reason=LLM_KEY_REASON_TOO_SHORT,
            length=len(key),
        )

    return LlmKeyStatus(configured=True, usable=True, reason=LLM_KEY_REASON_OK, length=len(key))


def is_llm_key_usable(api_key: str | None = None) -> bool:
    """:func:`evaluate_llm_api_key` 的布尔简写，语义完全一致。"""
    return evaluate_llm_api_key(api_key).usable


#: 所有 provider 凭据共用的占位前缀黑名单。**与
#: :data:`LLM_PLACEHOLDER_PREFIXES` 是同一个 tuple 对象**，不是第二份清单 ——
#: 仓库里关于"什么是占位 key"的规则只能有一处，否则就会出现 issue #51 那种
#: 两套实现各说各话的情况。
API_KEY_PLACEHOLDER_PREFIXES: tuple[str, ...] = LLM_PLACEHOLDER_PREFIXES


def is_placeholder_api_key(api_key: str | None) -> bool:
    """判定一个凭据是否**不可用于真实请求**（空 / 占位 / 示例值）。

    与 :func:`evaluate_llm_api_key` 的区别，以及为什么需要它
    --------------------------------------------------------
    ``evaluate_llm_api_key`` 只服务 LLM，且额外带一条 40 字符长度下限。本函数
    是**更窄**的判定：只回答"这串东西是不是显然的占位值"，**不**引入任何新的长度
    门槛。这一点对生产行为是硬约束：给 embedding / reranker 加一条长度规则，会
    让某个真实但较短的凭据突然失去向量通道 —— 那是行为变更，不是本 issue 的范围。
    本 issue（#52）要修的只是"占位 key 触发了真实外网请求"这一类。

    纯函数、无 IO：调用方可以放心地在健康面/启动面使用。

    Args:
        api_key: 凭据字符串；``None`` 视为未配置。

    Returns:
        ``True`` 表示**不应**用它发起真实 provider 请求（空串、空白串，或以任一
        :data:`API_KEY_PLACEHOLDER_PREFIXES` 开头的值，大小写不敏感）。
    """
    key = (api_key or "").strip()
    if not key:
        return True
    lowered = key.lower()
    return any(lowered.startswith(prefix) for prefix in API_KEY_PLACEHOLDER_PREFIXES)


def credential_may_egress(api_key: str | None) -> bool:
    """:func:`is_placeholder_api_key` 的取反，读起来更符合调用点语义。

    ``True`` 表示"这是一个看起来真实的凭据，可以（也只有这种情况可以）构造
    HTTP 客户端去打 provider"。
    """
    return not is_placeholder_api_key(api_key)


# ===== Provider 选择（issue #52：默认 lane 必须零外网依赖）=====
#
# 每个会发起外部请求的 RAG / 媒体依赖都有一个 provider 旋钮：
#
#   remote（默认）— 构造 HTTP 客户端，打真实 provider。**这是生产路径。**
#   local          — 使用进程内确定性实现（rag/local_provider.py），零网络、零凭据。
#
# `.env.test`（也就是 ``make test`` 用的 lane）把 EMBEDDING_PROVIDER /
# RERANKER_PROVIDER / STT_PROVIDER / TTS_PROVIDER 设为 local，所以默认测试套件
# 既不需要出网也不需要真实凭据。真实 provider 的验证走显式 opt-in lane
# （``make test-real-providers``）。
#
# 注意旋钮与凭据判定是**两个独立的闸门**，任一关闭就不发外部请求：
#   1. provider=local          → 走进程内实现，根本不构造 HTTP 客户端
#   2. 凭据是占位值（:func:`is_placeholder_api_key`）→ 不构造 HTTP 客户端
# 只看 truthiness 的旧写法（``if not EMBEDDING_API_KEY``）挡不住占位 key：
# ``sk-placeholder-...`` 是真值，于是会拿着假凭据去连 api.siliconflow.cn 并吃 401。
PROVIDER_REMOTE = "remote"
PROVIDER_LOCAL = "local"


def _normalize_provider(raw: str | None, *, default: str = PROVIDER_REMOTE) -> str:
    value = (raw or default).strip().lower()
    return value if value in (PROVIDER_REMOTE, PROVIDER_LOCAL) else default


# ===== 系统配置（放在 HTTP 配置之前，因为 HTTP_HEADERS 引用 VERSION）=====
VERSION = os.getenv("APP_VERSION", "6.3")  # 可从环境变量覆盖，便于 CI/CD
APP_NAME = os.getenv("APP_NAME", "Customer Service AI Agent")
DESCRIPTION = os.getenv(
    "APP_DESCRIPTION",
    "面向化妆品企业的多智能体客服系统（企业级增强版）",
)

# ===== HTTP 请求配置 =====
HTTP_TIMEOUT = _int_env("HTTP_TIMEOUT", 15)  # v5.4: 从 30s 降至 15s，减少失败场景等待
HTTP_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": f"MultiAgentCustomerService/{VERSION}",
}

# ===== 路由配置 =====
ROUTING_COMPLEXITY_THRESHOLD = _int_env("ROUTING_COMPLEXITY_THRESHOLD", 50)

# ===== v6.1: 缓存架构重构 =====
# L1: Redis 精确缓存
# （复用 REDIS_URL，不用新增变量）

# L2: Qdrant 语义缓存
CACHE_QDRANT_COLLECTION = os.getenv("CACHE_QDRANT_COLLECTION", "response_cache")
CACHE_QDRANT_MAX_POINTS = _int_env("CACHE_QDRANT_MAX_POINTS", 10000)
CACHE_VECTOR_SCORE_THRESHOLD = _float_env("CACHE_VECTOR_SCORE_THRESHOLD", 0.85)

# TTL 策略: intent_type → TTL(秒)
CACHE_TTL_POLICY = {
    "knowledge_qa": 604800,  # 7 天
    "pricing_stock": 300,  # 5 分钟
    "policy_rule": 86400,  # 24 小时
    "order_status": 300,  # 5 分钟
    "after_sales": 3600,  # 1 小时
    "chitchat": 600,  # 10 分钟
    "default": 3600,  # 1 小时
}

# L3: Jaccard 降级
CACHE_FALLBACK_ENABLED = os.getenv("CACHE_FALLBACK_ENABLED", "true").lower() == "true"
CACHE_FALLBACK_THRESHOLD = _float_env("CACHE_FALLBACK_THRESHOLD", 0.6)

# P0-02: 缓存内容版本。当知识库/提示词/产品目录发生大规模变更需要整体失效
# 旧缓存时，提升该版本即可使三层旧条目因版本不匹配而无法命中（无需显式迁移）。
# 注意：提升版本会使所有现有缓存条目失效（命中率短暂下降），属预期行为。
CACHE_CONTENT_VERSION = os.getenv("CACHE_CONTENT_VERSION", "1")

# 后台清理
CACHE_CLEANUP_INTERVAL = _int_env("CACHE_CLEANUP_INTERVAL", 3600)  # 秒

# 向后兼容别名（v6.1: 旧 env var 仍会读取，未设置时回退到新值）
CACHE_L1_MAX = _int_env("CACHE_L1_MAX", 500)  # 不再用于内存 L1，保留避免 ImportError
CACHE_L2_MAX = _int_env("CACHE_L2_MAX", 2000)  # 同上
CACHE_TTL = _int_env("CACHE_TTL", 3600)  # 仅作为 default_ttl 兼容
CACHE_SEMANTIC_THRESHOLD_SHORT = _float_env(
    "CACHE_SEMANTIC_THRESHOLD_SHORT", CACHE_VECTOR_SCORE_THRESHOLD
)
CACHE_SEMANTIC_THRESHOLD_LONG = _float_env(
    "CACHE_SEMANTIC_THRESHOLD_LONG", CACHE_VECTOR_SCORE_THRESHOLD
)

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
LLM_ROUTER_TIMEOUT = _float_env(
    "LLM_ROUTER_TIMEOUT", 4.0
)  # v4.3: 从 8s 降至 4s，配合熔断器快速 fallback

# ===== 重试配置 =====
RETRY_MAX_ATTEMPTS = _int_env("RETRY_MAX_ATTEMPTS", 3)  # v6.1: 3→指数退避可达 1s/2s/4s 三级
RETRY_BASE_DELAY = _float_env("RETRY_BASE_DELAY", 1.0)
RETRY_MAX_DELAY = _float_env("RETRY_MAX_DELAY", 30.0)  # v6.1: 指数退避最大延迟
RETRY_BACKOFF_FACTOR = _int_env("RETRY_BACKOFF_FACTOR", 2)  # v6.1: 指数退避因子

# ===== v3.4: 安全配置 ======
MAX_QUERY_LENGTH = _int_env("MAX_QUERY_LENGTH", 2000)
MAX_SESSIONS = _int_env("MAX_SESSIONS", 10000)
SESSION_IDLE_TTL = _int_env("SESSION_IDLE_TTL", 3600)

# ===== v3.7: 安全加固配置 =====
# 监控端点管理 Token（/api/metrics, /api/kpi 等敏感端点需要此 Token）
MONITORING_ADMIN_TOKEN = os.getenv("MONITORING_ADMIN_TOKEN", "")
#: 监控 token 的最小长度。**唯一真相源**：应用启动诊断（api/app_factory.py）与
#: 生成器（scripts/generate_monitoring_token.py）共用它，避免「生成器拒绝、
#: 运行时照收」的不一致。
MONITORING_ADMIN_TOKEN_MIN_LENGTH = 16
# WebSocket 连接限制
WS_MAX_CONNECTIONS_PER_IP = _int_env("WS_MAX_CONNECTIONS_PER_IP", 5)
WS_MESSAGE_RATE_LIMIT = _int_env("WS_MESSAGE_RATE_LIMIT", 10)
WS_IDLE_TIMEOUT = _int_env("WS_IDLE_TIMEOUT", 300)
# 会话令牌签名密钥（用于防会话劫持）
SESSION_TOKEN_SECRET = os.getenv("SESSION_TOKEN_SECRET", "")
# 会话数据加密密钥（用于文件/Redis 后端存储加密；为空则明文存储，向后兼容）
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY", "")
# 会话记录明文兼容策略（仅在 SESSION_ENCRYPTION_KEY 已配置时生效）：
#   legacy（默认）-> 允许读取启用加密**之前**写入的历史明文记录（可审计 + 读后按密文重写）；
#   reject         -> 严格模式：任何非密文记录一律拒绝（适用于已完成迁移的部署）。
# 未知取值视为配置错误并 fail closed（见 validate_session_encryption_settings）。
SESSION_ENCRYPTION_PLAINTEXT_COMPAT = (
    os.getenv("SESSION_ENCRYPTION_PLAINTEXT_COMPAT", "legacy").strip().lower()
)
SESSION_ENCRYPTION_PLAINTEXT_COMPAT_LEGACY = "legacy"
SESSION_ENCRYPTION_PLAINTEXT_COMPAT_REJECT = "reject"
SESSION_ENCRYPTION_PLAINTEXT_COMPAT_VALUES = (
    SESSION_ENCRYPTION_PLAINTEXT_COMPAT_LEGACY,
    SESSION_ENCRYPTION_PLAINTEXT_COMPAT_REJECT,
)
# TLS 配置
TLS_CERT_FILE = os.getenv("TLS_CERT_FILE", "")  # TLS 证书文件路径
TLS_KEY_FILE = os.getenv("TLS_KEY_FILE", "")  # TLS 私钥文件路径

# ===== v3.8: Token Quota 配置 =====
TOKEN_QUOTA_DAILY = _int_env("TOKEN_QUOTA_DAILY", 100000)  # 每日 Token 上限
TOKEN_QUOTA_MONTHLY = _int_env("TOKEN_QUOTA_MONTHLY", 2000000)  # 每月 Token 上限
TOKEN_QUOTA_ENABLED = os.getenv("TOKEN_QUOTA_ENABLED", "true").lower() == "true"  # 是否启用 Quota
TOKEN_QUOTA_REDIS_PREFIX = os.getenv("TOKEN_QUOTA_REDIS_PREFIX", "csai:quota:")  # Redis key 前缀


# ===== v6.1: 限流配置（供组件注册表引用）=====
RATE_LIMIT_MAX = _int_env("RATE_LIMIT_MAX", 60)  # 每分钟最大请求数
RATE_LIMIT_WINDOW = _int_env("RATE_LIMIT_WINDOW", 60)  # 时间窗口（秒）


# ===== v6.1: 基准测试 / 证据缺口修复配置 =====
BENCHMARK_REPORT_DIR = os.getenv("BENCHMARK_REPORT_DIR", "reports")
BENCHMARK_QUERY_COUNT = _int_env("BENCHMARK_QUERY_COUNT", 500)  # 默认评测集大小
BENCHMARK_SEED = _int_env("BENCHMARK_SEED", 42)  # 可复现随机种子
BENCHMARK_CACHE_WARMUP = _int_env("BENCHMARK_CACHE_WARMUP", 200)  # 缓存预热查询数
BENCHMARK_TOP_K = _int_env("BENCHMARK_TOP_K", 3)  # RAG 评估 Top-K
BENCHMARK_PREFETCH_ENABLED = os.getenv("BENCHMARK_PREFETCH_ENABLED", "true").lower() == "true"


# ===== 日志配置 =====
LOG_CONFIG = {
    "level": os.getenv("LOG_LEVEL", "INFO"),
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
}

# ===== CORS 配置（v3.0 新增） =====
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

# ===== API 认证配置（v3.0 新增） =====
# ===== 进程角色（config validation boundary） =====
#
# core/config.py 被**所有**进程导入：API 服务与 Celery worker。两者对配置的需求不同：
#   - API    : 需要 API Key 认证、CORS、端用户 JWT / Session 密钥；
#   - worker : 只执行 AgentRun，不终结 HTTP 请求、不签发或校验用户凭据，因此
#              API_KEY / CORS / JWT / SESSION_TOKEN_SECRET 对它没有意义。
#
# 过去 API 专属校验（尤其是下面这个 **import 期** 的硬 raise）会直接阻塞 worker 启动：
# worker 只要 import core.config 就必须提供 API_KEY，否则 ValueError 起不来，而 worker
# 根本不会读取这个配置。现在用 SERVICE_ROLE 显式区分，默认 `api` —— 生产 API 的行为
# 完全不变；只有显式声明 SERVICE_ROLE=worker 的进程才跳过 API 专属校验。
SERVICE_ROLE = os.getenv("SERVICE_ROLE", "api").strip().lower() or "api"
if SERVICE_ROLE not in ("api", "worker"):
    raise ConfigurationError(f"SERVICE_ROLE 非法: {SERVICE_ROLE!r}（仅支持 api | worker）")
#: 当前进程是否为 Celery worker（据此跳过 API 专属校验）。
IS_WORKER_PROCESS = SERVICE_ROLE == "worker"

API_KEY_ENABLED = os.getenv("API_KEY_ENABLED", "true").lower() == "true"
API_KEY = os.getenv("API_KEY", "")
# 仅 API 进程需要 API Key；worker 不终结 HTTP 请求，不应因缺 API_KEY 而无法启动。
if API_KEY_ENABLED and not API_KEY and not IS_WORKER_PROCESS:
    raise ValueError(
        "API_KEY_ENABLED=true requires a non-empty API_KEY. Set API_KEY in .env, "
        "disable authentication with API_KEY_ENABLED=false, or start this process with "
        "SERVICE_ROLE=worker if it is a Celery worker"
    )

# ===== ERP 模式配置（v3.0 新增） =====
ERP_MODE = os.getenv("ERP_MODE", "mock")  # mock | real
ERP_BASE_URL = os.getenv("ERP_BASE_URL", "")
ERP_APP_ID = os.getenv("ERP_APP_ID", "")
ERP_APP_SECRET = os.getenv("ERP_APP_SECRET", "")
ERP_DB_ID = os.getenv("ERP_DB_ID", "")

# P0-03 Remaining Risk #4: 权威 user_id → customer_id 映射（真实 ERP 模式）。
# 生产环境由认证系统 / ERP 客户绑定提供；此处通过环境变量配置 JSON 映射，
# 例如 ERP_USER_CUSTOMER_MAP='{"user_001":"C001","user_002":"C002"}'。
# 映射必须来自服务端可信数据，绝不来自 prompt / LLM / 资源自声明。
# 缺失 / 无效 / 非法时返回空 dict -> ErpAuthorizationService fail closed。
ERP_USER_CUSTOMER_MAP_RAW = os.getenv("ERP_USER_CUSTOMER_MAP", "")


def load_erp_user_customer_map(raw: str = ERP_USER_CUSTOMER_MAP_RAW) -> dict[str, str]:
    """解析 ERP_USER_CUSTOMER_MAP JSON 为 {user_id: customer_id} 映射。

    任何输入异常（空、非法 JSON、非 dict）一律返回空 dict（fail closed），
    绝不抛异常影响启动。值非字符串时强制转 str；null 值跳过。
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v is not None}


ERP_USER_CUSTOMER_MAP: dict[str, str] = load_erp_user_customer_map()

# ===== Redis 配置（v3.0 新增） =====
_redis_url_env = os.getenv("REDIS_URL", "").strip()
if _redis_url_env:
    REDIS_URL = _redis_url_env
else:
    _redis_password = os.getenv("REDIS_PASSWORD", "")
    _redis_host = os.getenv("REDIS_HOST", "localhost")
    REDIS_URL = (
        f"redis://:{_redis_password}@{_redis_host}:6379"
        if _redis_password
        else f"redis://{_redis_host}:6379"
    )

# ===== v3.5: RAG 配置 =====
RAG_PERSIST_DIRECTORY = os.getenv("RAG_PERSIST_DIRECTORY", "")  # 空则内存模式
RAG_N_RESULTS = _int_env("RAG_N_RESULTS", 3)
RAG_QUERY_REWRITING = (
    os.getenv("RAG_QUERY_REWRITING", "false").lower() == "true"
)  # v5.2: LLM 改写查询

# ===== v6.2: Embedding & Reranker API 配置（替代本地 sentence-transformers）=====
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
# v6.3: 当 EMBEDDING_API_KEY 未单独配置时，回退到 LLM 的 OPENAI_API_KEY
if not EMBEDDING_API_KEY:
    EMBEDDING_API_KEY = OPENAI_API_KEY
    logging.getLogger("config").info(
        "EMBEDDING_API_KEY 未配置，将复用 OPENAI_API_KEY。"
        "生产环境建议配置独立的 Embedding 服务 API Key"
    )
EMBEDDING_DIM = _int_env("EMBEDDING_DIM", 1024)  # bge-large-zh-v1.5 输出维度

RERANKER_BASE_URL = os.getenv("RERANKER_BASE_URL", "https://api.siliconflow.cn/v1")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "")

# ---- issue #52：这两个依赖的 provider 选择与出网许可 ----
#
# EMBEDDING_PROVIDER / RERANKER_PROVIDER = local 时使用进程内确定性实现，
# 零网络零凭据（见 rag/local_provider.py）。默认 remote = 生产行为不变。
EMBEDDING_PROVIDER = _normalize_provider(os.getenv("EMBEDDING_PROVIDER"))
RERANKER_PROVIDER = _normalize_provider(os.getenv("RERANKER_PROVIDER"))

#: 出网许可闸门。占位 key **不构成**出网许可 —— 旧代码只判 truthiness，
#: 而 ``sk-placeholder-embedding-test-key`` 是真值，于是默认测试 lane 会真的去
#: 连 api.siliconflow.cn 并吃 401（实测单次 ~40s，见 issue #52）。
#: 真实凭据仍然拿到 True，所以生产行为不变。
EMBEDDING_CREDENTIAL_USABLE = credential_may_egress(EMBEDDING_API_KEY)
RERANKER_CREDENTIAL_USABLE = credential_may_egress(RERANKER_API_KEY)

if not EMBEDDING_CREDENTIAL_USABLE:
    logging.getLogger("config").info(
        "EMBEDDING_API_KEY 是占位值或未配置：远程 embedding 通道不构造 HTTP 客户端"
        "（向量通道将不可用，检索诚实降级到 BM25/词法通道）。"
    )
if not RERANKER_CREDENTIAL_USABLE:
    logging.getLogger("config").info(
        "RERANKER_API_KEY 是占位值或未配置：远程 reranker 通道不构造 HTTP 客户端"
        "（重排降级为原始排序）。"
    )

# ===== Hybrid retrieval (vector + BM25) configuration =====
HYBRID_SEARCH_ENABLED = (
    os.getenv("HYBRID_SEARCH_ENABLED", "true").lower() == "true"
)  # 混合检索总开关
HYBRID_RRF_K = _int_env("HYBRID_RRF_K", 60)  # RRF rank 平滑常数
HYBRID_VECTOR_TOP_K = _int_env("HYBRID_VECTOR_TOP_K", 8)  # 向量通道每 collection top-K
HYBRID_BM25_TOP_K = _int_env("HYBRID_BM25_TOP_K", 8)  # BM25 通道每 collection top-K

# P1-02: BM25 lifecycle configuration.
BM25_REBUILD_TIMEOUT = _int_env("BM25_REBUILD_TIMEOUT", 60)
BM25_COLLECTIONS = ["product_knowledge", "faq", "tech_support", "complaint_knowledge"]

# ===== v6.0: Qdrant 向量数据库配置 =====
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = _int_env("QDRANT_PORT", 6333)  # REST API 端口
QDRANT_GRPC_PORT = _int_env("QDRANT_GRPC_PORT", 6334)
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
QDRANT_PREFER_GRPC = os.getenv("QDRANT_PREFER_GRPC", "false").lower() == "true"
QDRANT_COLLECTION_CONFIG = {
    "vectors": {"size": 1024, "distance": "Cosine"},
    "optimizers_config": {"default_segment_number": 2},
    "hnsw_config": {"m": 16, "ef_construct": 100},
}
# v6.2: 仅使用 Qdrant
VECTOR_DB_MODE = os.getenv("VECTOR_DB_MODE", "qdrant_only")

# ===== v3.5: ReAct 配置 =====
REACT_MAX_ITERATIONS = _int_env("REACT_MAX_ITERATIONS", 3)  # v4.3: 从 5 降至 3，控制延迟在 20s 内
REACT_COMPLEXITY_THRESHOLD = _int_env("REACT_COMPLEXITY_THRESHOLD", 60)
REACT_SELF_REFLECTION = os.getenv("REACT_SELF_REFLECTION", "false").lower() == "true"

# ===== v3.5: 工具调用配置 =====
TOOL_MAX_ROUNDS = _int_env("TOOL_MAX_ROUNDS", 3)

# Agent Context Engineering: deterministic Tool Result Context Budget.
TOOL_RESULT_OPTIMIZATION_ENABLED = (
    os.getenv("TOOL_RESULT_OPTIMIZATION_ENABLED", "false").lower() == "true"
)
TOOL_RESULT_MAX_TOKENS = _int_env("TOOL_RESULT_MAX_TOKENS", 800)
TOOL_RESULT_MAX_ITEMS = _int_env("TOOL_RESULT_MAX_ITEMS", 5)
TOOL_RESULT_PRESERVE_RECENT = _int_env("TOOL_RESULT_PRESERVE_RECENT", 2)
TOOL_RESULT_OFFLOAD_ENABLED = os.getenv("TOOL_RESULT_OFFLOAD_ENABLED", "false").lower() == "true"
TOOL_RESULT_OFFLOAD_MIN_TOKENS = _int_env("TOOL_RESULT_OFFLOAD_MIN_TOKENS", 1200)
TOOL_RESULT_STORE_TTL_SECONDS = _int_env("TOOL_RESULT_STORE_TTL_SECONDS", 900)
TOOL_RESULT_SEMANTIC_SUMMARY_ENABLED = (
    os.getenv("TOOL_RESULT_SEMANTIC_SUMMARY_ENABLED", "false").lower() == "true"
)
TOOL_RESULT_CACHE_ENABLED = os.getenv("TOOL_RESULT_CACHE_ENABLED", "false").lower() == "true"

# 原生工具的单次执行超时（秒）。0 = 不限制（不建议）。
#
# 为什么需要它：此前**只有** MCP 工具有 per-call timeout（tools/mcp_adapter.py），
# 原生工具（ERP / 内部查询）完全没有超时边界。一个卡住的 ERP 连接会一直占着
# Agent 的工具循环、run 的租约与 thread lock，直到 Celery 的 task time limit
# 把整个 run 掐掉 —— 单个慢工具升级成「整条 run 失败 + 重投」。
#
# 取值应当显著小于 AGENT_RUN_TASK_TIME_LIMIT：工具超时是**局部**降级（返回一条
# 可解释的错误，让 Agent 换策略或如实告知用户），而 task time limit 是**整体**失败。
# 校验见 validate_tool_settings。
TOOL_EXECUTION_TIMEOUT_SECONDS = _float_env("TOOL_EXECUTION_TIMEOUT_SECONDS", 30.0)

# ===== MCP（Model Context Protocol）外部工具接入 =====
# 默认**关闭**。MCP 工具是来自外部进程（可能是第三方）的不可信输入源，
# 与进程内 native 工具的信任级别不同，因此必须显式开启 + 显式 allowlist。
#
# MCP_SERVERS 是 JSON 数组 allowlist，每条形如：
#   {"name": "catalog", "transport": "stdio", "command": "python3",
#    "args": ["-m", "my_mcp_server"], "allowed_tools": ["get_product_info"],
#    "timeout_seconds": 15.0, "max_payload_bytes": 32768,
#    "enabled": true, "risk_level": "low"}
#
# ``risk_level`` 与 ``core.hitl.risk.RiskLevel`` **同源**，只能取
# ``low`` / ``medium`` / ``high``（见 MCP_RISK_LEVELS），不另立词汇表。
#
# **read-only-first**：只有显式声明 ``"low"`` 的 server 的工具才会被注册
# （见 tools/mcp_adapter.py::register_mcp_tools）。缺失或非法的 ``risk_level``
# 一律收敛到 ``HIGH``（只向上收敛，绝不向下），因此其工具**不会**进入
# Function Calling 表面。本模块尚未为写操作接通幂等 ledger 与人工审批链路，
# 接入写操作 MCP 工具必须先补这两道防线。
MCP_ENABLED = os.getenv("MCP_ENABLED", "false").lower() == "true"
MCP_SERVERS = os.getenv("MCP_SERVERS", "")
MCP_DEFAULT_TIMEOUT_SECONDS = _float_env("MCP_DEFAULT_TIMEOUT_SECONDS", 15.0)
MCP_MAX_PAYLOAD_BYTES = _int_env("MCP_MAX_PAYLOAD_BYTES", 32768)
# 初始化失败时是否 fail closed（抛出，阻止服务启动）还是降级为 native-only。
# 生产默认应设 true：静默降级会让「以为 MCP 已生效」的运维假设与实际不符。
MCP_FAIL_CLOSED = os.getenv("MCP_FAIL_CLOSED", "false").lower() == "true"

# ===== v4.0: 用户认证配置 =====
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_EXPIRE_HOURS = _int_env("JWT_EXPIRE_HOURS", 72)
JWT_ACCESS_EXPIRE_HOURS = _int_env("JWT_ACCESS_EXPIRE_HOURS", 2)  # P2-3: access_token 短生命周期
JWT_REFRESH_EXPIRE_HOURS = _int_env("JWT_REFRESH_EXPIRE_HOURS", 168)  # P2-3: refresh_token 7天
# v4.0: 本地开发模式（仅开发环境设置此变量为 true，生产环境禁止）
DEV_MODE = os.getenv("DEV_MODE", "").lower() == "true"

# ===== v4.0: 数据库配置 =====
DB_DIR = os.getenv("DB_DIR", "data")
DB_PATH = os.getenv("DB_PATH", os.path.join(DB_DIR, "csai.db"))

# ===== v4.0: 告警通知配置 =====
ALERT_WEBHOOKS = os.getenv(
    "ALERT_WEBHOOKS", ""
)  # JSON 数组: [{"name":"钉钉","url":"...","type":"dingtalk"}]
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = _int_env("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")

# ===== v4.1: 数据库配置（PostgreSQL + SQLite 兼容）=====
DATABASE_URL = os.getenv("DATABASE_URL", "")  # 为空则使用 SQLite
ALEMBIC_CONFIG_PATH = os.getenv("ALEMBIC_CONFIG_PATH", "alembic.ini")

# ===== LangGraph Checkpoint 持久化配置 =====
# 背景：MemorySaver 只存在于当前 Python 进程内，gunicorn 多 worker / 多副本
# 之间不共享，实例重启即丢失。生产需要官方 PostgreSQL checkpointer。
#   memory   -> langgraph.checkpoint.memory.MemorySaver（进程内，仅开发/测试）
#   postgres -> langgraph.checkpoint.postgres.AsyncPostgresSaver（跨进程/重启持久化）
# 留空表示按环境自动选择：开发/测试->memory，生产->postgres。
LANGGRAPH_CHECKPOINT_BACKEND_RAW = os.getenv("LANGGRAPH_CHECKPOINT_BACKEND", "").strip().lower()
# 显式 checkpoint 数据库 URL。留空且 backend=postgres 时安全复用 DATABASE_URL
# （仅接受 libpq 兼容的 postgresql:// / postgres://，剥离 SQLAlchemy +driver 后缀；
#  SQLite / 其它协议一律拒绝，绝不静默降级）。
LANGGRAPH_CHECKPOINT_DATABASE_URL = os.getenv("LANGGRAPH_CHECKPOINT_DATABASE_URL", "").strip()
# psycopg 连接池大小与首次 setup 超时（秒）
LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE = _int_env("LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE", 1)
LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE = _int_env("LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE", 10)
LANGGRAPH_CHECKPOINT_SETUP_TIMEOUT = _float_env("LANGGRAPH_CHECKPOINT_SETUP_TIMEOUT", 15.0)


def resolve_checkpoint_backend(raw: str = "", *, dev_mode: bool = False) -> str:
    """解析 checkpoint backend，非法值 fail closed。

    空值按环境选择（开发/测试 memory，生产 postgres）；显式值只允许
    ``memory`` / ``postgres``，其它值抛 ``ConfigurationError``。
    """
    value = (raw or "").strip().lower()
    if not value:
        return "memory" if dev_mode else "postgres"
    if value not in ("memory", "postgres"):
        raise ConfigurationError(
            f"LANGGRAPH_CHECKPOINT_BACKEND 非法: {value!r}（仅支持 memory | postgres）"
        )
    return value


def derive_checkpoint_database_url(explicit: str = "", *, database_url: str = "") -> str | None:
    """把 SQLAlchemy 风格 DSN 安全转换为 psycopg 可用的 libpq DSN。

    只接受 postgres 协议；剥离 ``postgresql+psycopg2://`` 这类 SQLAlchemy
    driver 后缀，标准化 ``postgres://`` -> ``postgresql://``。SQLite / MySQL /
    无 scheme 的输入返回 ``None``（由调用方 fail closed），不做字符串乱替换。
    """
    raw = (explicit or "").strip() or (database_url or "").strip()
    if not raw:
        return None
    scheme, sep, rest = raw.partition("://")
    if not sep:
        return None
    base = scheme.split("+", 1)[0].lower()
    if base in ("postgres", "postgresql"):
        return f"postgresql://{rest}"
    return None


LANGGRAPH_CHECKPOINT_BACKEND = resolve_checkpoint_backend(
    LANGGRAPH_CHECKPOINT_BACKEND_RAW, dev_mode=DEV_MODE
)


def validate_checkpoint_settings(
    backend: str, *, explicit_url: str = "", database_url: str = "", dev_mode: bool = False
) -> list[str]:
    """校验 checkpoint 配置，返回错误信息列表（空 = 通过）。

    生产（``dev_mode=False``）：
      - 禁止 ``memory``（进程内、不跨 worker/副本、重启丢失）；
      - ``postgres`` 必须能解析出 PostgreSQL DSN（显式 URL 或 DATABASE_URL）。
    开发/测试：不做约束（允许显式 memory / 自动 memory）。
    """
    problems: list[str] = []
    if dev_mode:
        return problems
    if backend == "memory":
        problems.append(
            "Production requires LANGGRAPH_CHECKPOINT_BACKEND=postgres "
            "(MemorySaver is process-local and not shared across workers/replicas)"
        )
    elif backend == "postgres" and not derive_checkpoint_database_url(
        explicit_url, database_url=database_url
    ):
        problems.append(
            "Production checkpoint backend=postgres requires a PostgreSQL "
            "LANGGRAPH_CHECKPOINT_DATABASE_URL or a PostgreSQL DATABASE_URL"
        )
    return problems


# ===== 分布式 Agent Runtime（异步 Run + Celery Worker）=====
# AgentRun 业务状态真相源是数据库；Redis/Celery 仅调度。
#
# ---- 执行模式（canonical 开关）----
#
# AGENT_EXECUTION_MODE 决定的是**异步 Run 路径**（``POST /api/runs``）在哪里执行：
#   inline : API 进程内执行。适合开发/测试；**不是** durable —— 进程崩溃该 run
#            直接丢失，且没有 worker 重投 / lease 接管 / 事件流。
#   queued : 落库 + 入队，由独立 Celery worker 执行。生产推荐。
#
# 它**不影响** 快路径 ``/api/chat``、``/api/chat/stream``、``/api/chat/multimodal``：
# 那些接口是既有的**兼容性 inline 路径**，始终在 API 进程内执行 LangGraph
# （见 ``api/app.py::_run_graph``）。把流式接口改走队列会改变延迟与响应结构，
# 因此本仓库**不做**这种改写；不要宣称"所有请求都经过 Worker"。
#
# AGENT_RUN_DISPATCH 是同义的历史变量（celery=inline 的旧命名）。未显式设置
# AGENT_EXECUTION_MODE 时由它派生，两者冲突时报错，避免出现两个互相矛盾的旋钮。
AGENT_EXECUTION_MODE_RAW = os.getenv("AGENT_EXECUTION_MODE", "").strip().lower()
AGENT_RUN_DISPATCH_RAW = os.getenv("AGENT_RUN_DISPATCH", "").strip().lower()

# dispatch: celery（生产，解耦到 worker）| inline（开发/测试 fallback，进程内执行）
AGENT_RUN_DISPATCH = AGENT_RUN_DISPATCH_RAW or "celery"
AGENT_RUN_QUEUE = os.getenv("AGENT_RUN_QUEUE", "agent_runs").strip() or "agent_runs"
AGENT_RUN_MAX_ATTEMPTS = _int_env("AGENT_RUN_MAX_ATTEMPTS", 3)
AGENT_RUN_TASK_SOFT_TIME_LIMIT = _int_env("AGENT_RUN_TASK_SOFT_TIME_LIMIT", 120)
AGENT_RUN_TASK_TIME_LIMIT = _int_env("AGENT_RUN_TASK_TIME_LIMIT", 180)
# Redis broker 未 ACK 消息的 visibility timeout（秒）；需 > 最长任务时间
AGENT_RUN_VISIBILITY_TIMEOUT = _int_env("AGENT_RUN_VISIBILITY_TIMEOUT", 3600)
# Celery broker 默认复用 REDIS_URL；result backend 留空表示不落结果（非真相源）
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "").strip() or REDIS_URL
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "").strip()

if AGENT_RUN_DISPATCH not in ("celery", "inline"):
    raise ConfigurationError(
        f"AGENT_RUN_DISPATCH 非法: {AGENT_RUN_DISPATCH!r}（仅支持 celery | inline）"
    )


def _resolve_execution_mode() -> str:
    """归一化 ``AGENT_EXECUTION_MODE`` / ``AGENT_RUN_DISPATCH`` 为单一权威值。

    优先级：显式设置的 ``AGENT_EXECUTION_MODE`` **优先**于历史变量
    ``AGENT_RUN_DISPATCH``。两者不一致时**告警但不拒绝启动**——否则仓库 ``.env``
    里的 ``AGENT_RUN_DISPATCH=inline``（开发默认）会让任何
    ``AGENT_EXECUTION_MODE=queued`` 配置直接起不来。非法取值仍然 fail-fast。
    """
    if not AGENT_EXECUTION_MODE_RAW:
        # 未设置 canonical 旋钮：沿用历史变量
        return "queued" if AGENT_RUN_DISPATCH == "celery" else "inline"
    if AGENT_EXECUTION_MODE_RAW not in ("inline", "queued"):
        raise ConfigurationError(
            f"AGENT_EXECUTION_MODE 非法: {AGENT_EXECUTION_MODE_RAW!r}" "（仅支持 inline | queued）"
        )
    expected_dispatch = "celery" if AGENT_EXECUTION_MODE_RAW == "queued" else "inline"
    if AGENT_RUN_DISPATCH_RAW and expected_dispatch != AGENT_RUN_DISPATCH:
        import warnings

        warnings.warn(
            f"AGENT_EXECUTION_MODE={AGENT_EXECUTION_MODE_RAW!r} 覆盖 "
            f"AGENT_RUN_DISPATCH={AGENT_RUN_DISPATCH!r}（canonical 旋钮优先；"
            "AGENT_RUN_DISPATCH 仅作向后兼容，建议只保留 AGENT_EXECUTION_MODE）",
            RuntimeWarning,
            stacklevel=2,
        )
    return AGENT_EXECUTION_MODE_RAW


AGENT_EXECUTION_MODE = _resolve_execution_mode()
# canonical 模式是权威值，据此同步实际派发方式（worker 执行入口读 AGENT_RUN_DISPATCH）
AGENT_RUN_DISPATCH = "celery" if AGENT_EXECUTION_MODE == "queued" else "inline"

#: 快路径接口始终 inline（不由 AGENT_EXECUTION_MODE 控制）。
#: 仅用于文档/健康检查自述，避免出现"所有请求都经过 Worker"的误读。
AGENT_INLINE_COMPAT_ENDPOINTS = ("/api/chat", "/api/chat/stream", "/api/chat/multimodal")
AGENT_QUEUED_RUN_ENDPOINTS = ("/api/runs",)

# Thread 串行执行锁（同一 thread 的 Run 不得并发；不同 thread 可并发）
AGENT_RUN_THREAD_LOCK_ENABLED = os.getenv("AGENT_RUN_THREAD_LOCK_ENABLED", "true").lower() == "true"
AGENT_RUN_THREAD_LOCK_BACKEND = os.getenv("AGENT_RUN_THREAD_LOCK_BACKEND", "redis").strip().lower()
AGENT_RUN_THREAD_LOCK_TTL_SECONDS = _float_env("AGENT_RUN_THREAD_LOCK_TTL_SECONDS", 300.0)
AGENT_RUN_THREAD_LOCK_RETRY_DELAY_SECONDS = _float_env(
    "AGENT_RUN_THREAD_LOCK_RETRY_DELAY_SECONDS", 5.0
)
# API 执行边界获取锁的最大等待时间（秒）；超时返回 THREAD_BUSY（HTTP 409）
AGENT_RUN_THREAD_LOCK_ACQUIRE_TIMEOUT_SECONDS = _float_env(
    "AGENT_RUN_THREAD_LOCK_ACQUIRE_TIMEOUT_SECONDS", 5.0
)
# worker ownership lease / heartbeat（崩溃后 lease 过期可被接管）
AGENT_RUN_LEASE_SECONDS = _float_env("AGENT_RUN_LEASE_SECONDS", 180.0)
AGENT_RUN_HEARTBEAT_SECONDS = _float_env("AGENT_RUN_HEARTBEAT_SECONDS", 30.0)
# 重试退避（指数 + jitter）
AGENT_RUN_RETRY_BASE_DELAY = _float_env("AGENT_RUN_RETRY_BASE_DELAY", 2.0)
AGENT_RUN_RETRY_MAX_DELAY = _float_env("AGENT_RUN_RETRY_MAX_DELAY", 60.0)
AGENT_RUN_RETRY_JITTER = _float_env("AGENT_RUN_RETRY_JITTER", 0.3)

# 卡死 run 兜底扫描的调度周期。
#
# **为什么必须有它（实测依据）**：Celery 只在 `WorkerLostError`（worker 进程被杀）
# 时才把消息重新投递。普通 Exception、`RetryPublicationError`、软/硬超时时间上限
# 全部落到 `Request.on_failure` 的 `self.reject(requeue=False)` 分支——**消息被丢弃，
# 不重投**（真实 Redis + Celery 5.6.3 实测，见
# tests/integration/runtime/test_celery_delivery_semantics.py）。因此「让异常逃逸以
# 触发 redelivery」这个前提是错的：逃逸只是把消息丢掉，run 会停在
# QUEUED/RETRYING 且**没有任何消息会再来**。
#
# 所以恢复闭环的承重结构是「数据库里的 AgentRun + 定时扫描」，队列消息只是唤醒信号，
# 而唤醒信号随时可能丢。扫描未调度 = 没有恢复闭环。
AGENT_RUN_RECONCILE_INTERVAL_SECONDS = _float_env("AGENT_RUN_RECONCILE_INTERVAL_SECONDS", 30.0)

# ---------------------------------------------------------------------------
# Human-in-the-loop（高风险工具副作用的人工审批治理）
# ---------------------------------------------------------------------------
# 总开关。关闭时 HIGH 风险工具**直接执行**（不拦），仅 MEDIUM/LOW 记录。
# 生产建议保持 true：关闭等于主动放弃该治理边界。
HITL_ENABLED = os.getenv("HITL_ENABLED", "false").lower() == "true"

# 强制人工审批的工具名（逗号分隔，大小写不敏感）
HITL_HIGH_RISK_TOOLS = os.getenv("HITL_HIGH_RISK_TOOLS", "")
# 只记录不拦截的工具名（逗号分隔）
HITL_MEDIUM_RISK_TOOLS = os.getenv("HITL_MEDIUM_RISK_TOOLS", "")

# 金额阈值：提案参数中任一金额字段 >= 该值即判为 HIGH（覆盖工具名白名单之外的
# 大额写操作）。0 = 关闭金额维度。
HITL_HIGH_AMOUNT_THRESHOLD = _float_env("HITL_HIGH_AMOUNT_THRESHOLD", 0.0)

# 审批有效期：超时未决策 -> EXPIRED（等同拒绝，但可与「明确拒绝」区分统计）。
HITL_APPROVAL_TTL_SECONDS = _float_env("HITL_APPROVAL_TTL_SECONDS", 3600.0)

# 可审批的角色（4 级 RBAC 中的高级角色）。customer 永不拥有审批权。
HITL_REVIEWER_ROLES = tuple(
    r.strip().lower()
    for r in os.getenv("HITL_REVIEWER_ROLES", "admin,supervisor").split(",")
    if r.strip()
)

# ---------------------------------------------------------------------------
if AGENT_RUN_THREAD_LOCK_BACKEND not in ("redis", "memory"):
    raise ConfigurationError(
        f"AGENT_RUN_THREAD_LOCK_BACKEND 非法: {AGENT_RUN_THREAD_LOCK_BACKEND!r}"
        "（仅支持 redis | memory）"
    )


def validate_app_database_dsn(*, dev_mode: bool, database_url: str) -> list[str]:
    """生产应用库 DSN 校验（纯函数，返回问题列表，空 = 通过）。

    为什么需要它
    ------------
    ``side_effect`` ledger 的接管路径依赖数据库**行级锁**排除并发重复执行。
    只有 PostgreSQL（``SELECT ... FOR UPDATE``）提供该保证；SQLite / MySQL 的
    ``FOR UPDATE`` 被忽略，两个认领者可同时接管同一笔写（Issue #13）。运行时已
    fail-closed，但生产应更早失败——拒绝带病启动。

    注意 ``LANGGRAPH_CHECKPOINT_DATABASE_URL`` 是**独立**设置，因此
    「PG checkpoint + SQLite 应用库」会骗过只看 checkpoint 的校验；这里对
    **应用库**单独把关。开发/测试不受约束（SQLite 是默认的回退库）。
    """
    if dev_mode:
        return []
    if not database_url:
        return ["Production requires DATABASE_URL (PostgreSQL)"]
    if not database_url.startswith(("postgresql://", "postgres://")):
        return [
            "Production DATABASE_URL must be PostgreSQL (got a non-PostgreSQL DSN): "
            "the side-effect ledger's takeover path requires row-level locking; "
            "SQLite/MySQL cannot provide it (see Issue #13)"
        ]
    return []


def validate_distributed_runtime_settings(
    *,
    dev_mode: bool,
    session_backend: str,
    checkpoint_backend: str,
    gunicorn_workers: int,
    lock_enabled: bool,
    lock_backend: str,
    agent_run_dispatch: str = "celery",
    lock_ttl_seconds: float = 300.0,
    task_time_limit_seconds: float = 180.0,
    ttl_safety_margin_seconds: float = 30.0,
) -> list[str]:
    """生产分布式运行时一致性校验（纯函数，返回问题列表，空 = 通过）。

    规则：
      - 生产必须 Redis Session（进程内 memory 在多 worker 下分片、重启丢失）；
      - 生产异步 Run 必须 `AGENT_RUN_DISPATCH=celery`（inline 在 API 进程内执行，
        失去 worker 解耦，不能用于生产）；
      - gunicorn 多 worker 时要求 postgres checkpoint + redis session +
        分布式 thread lock，否则跨 worker 正确性无法保证；
      - thread lock TTL 必须大于 task time limit + safety margin，否则任务仍在执行时
        锁就可能过期（stale worker 边界，见 docs）。
    开发/测试不做约束。
    """
    problems: list[str] = []
    if dev_mode:
        return problems
    if session_backend != "redis":
        problems.append(
            "Production requires SESSION_STORAGE_BACKEND=redis "
            f"(got {session_backend!r}); in-process memory sessions are not shared "
            "across gunicorn workers/replicas and are lost on restart"
        )
    if agent_run_dispatch != "celery":
        problems.append(
            "Production requires AGENT_RUN_DISPATCH=celery "
            f"(got {agent_run_dispatch!r}); inline runs execute inside the API "
            "process and lose worker decoupling"
        )
    if gunicorn_workers > 1:
        if checkpoint_backend != "postgres":
            problems.append(
                f"GUNICORN_WORKERS={gunicorn_workers}>1 requires "
                "LANGGRAPH_CHECKPOINT_BACKEND=postgres (memory checkpoint is per-process)"
            )
        if session_backend != "redis":
            problems.append(
                f"GUNICORN_WORKERS={gunicorn_workers}>1 requires SESSION_STORAGE_BACKEND=redis"
            )
        if not lock_enabled or lock_backend != "redis":
            problems.append(
                f"GUNICORN_WORKERS={gunicorn_workers}>1 requires "
                "AGENT_RUN_THREAD_LOCK_ENABLED=true and "
                "AGENT_RUN_THREAD_LOCK_BACKEND=redis (per-thread cross-process lock)"
            )
    if lock_ttl_seconds <= task_time_limit_seconds + ttl_safety_margin_seconds:
        problems.append(
            "AGENT_RUN_THREAD_LOCK_TTL_SECONDS "
            f"({lock_ttl_seconds}) must exceed AGENT_RUN_TASK_TIME_LIMIT "
            f"({task_time_limit_seconds}) + safety margin ({ttl_safety_margin_seconds}); "
            "otherwise the lock can expire while the task is still running"
        )
    return problems


# ===== v4.1: SSE 流式输出配置 =====
SSE_CHUNK_SIZE = _int_env("SSE_CHUNK_SIZE", 50)  # 每次发送的字符数
SSE_ENABLED = os.getenv("SSE_ENABLED", "true").lower() == "true"

# ===== v4.1: 多模态配置 =====
MULTIMODAL_ENABLED = os.getenv("MULTIMODAL_ENABLED", "false").lower() == "true"

# ---- issue #52：STT / TTS 的 provider 选择与出网许可 ----
#
# STT（Whisper）与 embedding/reranker 一样是**带凭据**的 HTTP 依赖，因此复用
# OPENAI_API_KEY，并复用 :func:`is_llm_key_usable` 这一权威判定。
STT_PROVIDER = _normalize_provider(os.getenv("STT_PROVIDER"))
STT_CREDENTIAL_USABLE = is_llm_key_usable(OPENAI_API_KEY)

# TTS 走 edge_tts —— 一个**无凭据**的第三方端点（speech.platform.bing.com）。
# 所以它没有"key 是否可用"这个问题，只有"这条 lane 是否允许出网"。
TTS_PROVIDER = _normalize_provider(os.getenv("TTS_PROVIDER"))
MAX_IMAGE_SIZE_MB = _int_env("MAX_IMAGE_SIZE_MB", 5)
ALLOWED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"]

# ===== v5.1: Vision LLM 配置（多模态模型）=====
VISION_MODEL = os.getenv("VISION_MODEL", "")  # 留空则复用 OPENAI_MODEL
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "")  # 留空则复用 OPENAI_BASE_URL
VISION_API_KEY = os.getenv("VISION_API_KEY", "")  # 留空则复用 OPENAI_API_KEY

# ===== v5.1: CLIP 多模态检索配置 =====
CLIP_ENABLED = os.getenv("CLIP_ENABLED", "false").lower() == "true"
IMAGE_COLLECTION_NAME = os.getenv("IMAGE_COLLECTION_NAME", "image_knowledge")

# ===== v4.1: Redis 用途扩展 =====
REDIS_JWT_PREFIX = os.getenv("REDIS_JWT_PREFIX", "csai:jwt:blacklist:")
REDIS_RATE_PREFIX = os.getenv("REDIS_RATE_PREFIX", "csai:rate:")
REDIS_SESSION_PREFIX = os.getenv("REDIS_SESSION_PREFIX", "csai:session:")

# ===== v4.1: A/B 测试配置 =====
AB_TEST_ENABLED = os.getenv("AB_TEST_ENABLED", "false").lower() == "true"

# ===== v4.1: 自我评估配置 =====
EVAL_LOW_SCORE_THRESHOLD = _int_env("EVAL_LOW_SCORE_THRESHOLD", 40)  # 低于此分触发告警
EVAL_ALERT_ENABLED = os.getenv("EVAL_ALERT_ENABLED", "true").lower() == "true"

# ===== v4.3: 低分重试与模式升级配置 =====
EVAL_RETRY_THRESHOLD = _int_env("EVAL_RETRY_THRESHOLD", 30)  # 低于此分触发自动重试/升级
EVAL_RETRY_ENABLED = os.getenv("EVAL_RETRY_ENABLED", "true").lower() == "true"
MODE_UPGRADE_ENABLED = os.getenv("MODE_UPGRADE_ENABLED", "true").lower() == "true"

# ===== v4.3: 各协作模式独立 SLA 超时（秒）=====
SLA_SEQUENTIAL_MAX = _float_env("SLA_SEQUENTIAL_MAX", 15.0)
SLA_PARALLEL_MAX = _float_env("SLA_PARALLEL_MAX", 20.0)
SLA_CONSULTATION_MAX = _float_env("SLA_CONSULTATION_MAX", 25.0)
SLA_HIERARCHICAL_MAX = _float_env("SLA_HIERARCHICAL_MAX", 30.0)
SLA_REACT_MAX = _float_env("SLA_REACT_MAX", 30.0)


def validate_mcp_settings(
    *,
    enabled: bool = MCP_ENABLED,
    servers: str = MCP_SERVERS,
    max_payload_bytes: int = MCP_MAX_PAYLOAD_BYTES,
) -> list[str]:
    """校验 MCP 配置（纯函数，无 IO），返回错误信息列表（空 = 通过）。

    只做**结构性**检查，不去连接任何 server：这里的职责是让明显不可用的配置在
    启动时炸掉，而不是等到第一次工具调用才失败。

    刻意不检查的东西：``MCP_SERVERS`` 里的每条记录是否合法由
    ``tools.mcp_adapter.load_mcp_server_configs`` 负责（它对单条非法记录是
    跳过 + 记日志，而非整体拒绝），避免同一个 JSON 在两处用两套规则解析。
    """
    if not enabled:
        return []

    errors: list[str] = []
    text = (servers or "").strip()
    if not text:
        errors.append("MCP_ENABLED=true 但 MCP_SERVERS 为空：没有 allowlist 就没有可连接的 server")
        return errors
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        errors.append(f"MCP_SERVERS 不是合法 JSON: {type(e).__name__}")
        return errors
    if not isinstance(data, list):
        errors.append(f"MCP_SERVERS 必须是 JSON 数组，实际是 {type(data).__name__}")
        return errors
    if not data:
        errors.append("MCP_SERVERS 是空数组：等于没有声明任何 server")
        return errors
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            errors.append("MCP_SERVERS 中存在缺少 name 的条目")
            break
        errors.extend(_validate_mcp_risk_level(item))
    if max_payload_bytes <= 0:
        errors.append(f"MCP_MAX_PAYLOAD_BYTES 必须为正数（当前 {max_payload_bytes}）")
    return errors


#: MCP server 允许显式声明的风险等级。**与 ``core.hitl.risk.RiskLevel`` 同源**，
#: 这里只做字面量校验，不另立词汇表。
MCP_RISK_LEVELS = ("low", "medium", "high")


def _validate_mcp_risk_level(item: dict) -> list[str]:
    """MCP server ``risk_level`` 的结构性校验（fail closed 的方向是"响亮地失败"）。

    运行时（``tools.mcp_adapter._coerce_risk_level``）会把缺失 / 非法的
    ``risk_level`` 一律收敛到 ``HIGH``，绝不会放行。这里额外在**启动期**报出来，
    是为了让"我以为配了 low、实际拼错了"或"压根忘了配"这两种错误在部署时就可见，
    而不是等到工具列表莫名为空才发现。
    """
    name = item.get("name")
    raw = item.get("risk_level")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return [
            f"MCP server {name!r} 未显式声明 risk_level —— 按 'high' 处理，"
            '该 server 的工具不会被注册；若确认只读请加 "risk_level": "low"'
        ]
    if not isinstance(raw, str) or raw.strip().lower() not in MCP_RISK_LEVELS:
        return [
            f"MCP server {name!r} 的 risk_level 非法（{raw!r}）—— 期望 "
            f"{MCP_RISK_LEVELS} 之一；将按 'high' 处理（工具不会被注册）"
        ]
    return []


def _default_side_effect_claim_ttl() -> float:
    """从 ledger 模块取实际生效的 claim TTL；导入失败退回其默认值。

    刻意**不在 config 里复制常量**：两边各写一个数字会悄悄漂移，而这条不变量正是
    为了防止漂移。取不到时用 ledger 自己的默认值（两边一致）。
    """
    try:
        from runtime.side_effects import DEFAULT_CLAIM_TTL_SECONDS

        return float(DEFAULT_CLAIM_TTL_SECONDS)
    except Exception:
        return 60.0


def _claim_ttl_margin_seconds() -> float:
    """claim TTL 的余量；唯一真相源在 ledger 模块（见 ``min_claim_ttl_seconds``）。

    依赖方向刻意是单向的（config -> runtime.side_effects），与
    ``_default_side_effect_claim_ttl`` 一致。两边各写一个数字就是这条不变量要防的
    漂移，而反向依赖会形成真正的 import 环。
    """
    try:
        from runtime.side_effects import CLAIM_TTL_MIN_MARGIN_SECONDS

        return float(CLAIM_TTL_MIN_MARGIN_SECONDS)
    except Exception:
        return 30.0


def validate_timeout_invariants(
    *,
    lease_seconds: float = AGENT_RUN_LEASE_SECONDS,
    heartbeat_seconds: float = AGENT_RUN_HEARTBEAT_SECONDS,
    task_time_limit_seconds: float = float(AGENT_RUN_TASK_TIME_LIMIT),
    visibility_timeout_seconds: float = float(AGENT_RUN_VISIBILITY_TIMEOUT),
    tool_timeout_seconds: float = TOOL_EXECUTION_TIMEOUT_SECONDS,
    thread_lock_ttl_seconds: float = AGENT_RUN_THREAD_LOCK_TTL_SECONDS,
    claim_ttl_seconds: float | None = None,
    lock_enabled: bool = AGENT_RUN_THREAD_LOCK_ENABLED,
    task_time_limit_enabled: bool = True,
) -> list[str]:
    """超时/租约交叉不变量（纯函数，返回**致命**问题列表，空 = 通过）。

    与其它校验的区别
    ----------------
    ``validate_distributed_runtime_settings`` 只在生产生效；本函数在**所有**环境
    生效，理由与 HITL 校验一致：这些关系错了会在运行时产生**不确定的重复执行**
    （重复投递、重复写、锁丢失），与「是不是生产」无关。开发机上的集成测试拿到
    虚假的安全性同样有害。

    必须强制的正确性约束（不满足 -> 拒绝启动）
    ------------------------------------------
    1. ``heartbeat * 2 < lease``：续租的可证明条件。周期续租的架构**不要求**
       lease 大于整个任务时长——那会让长任务永远拿不到执行权。要求的是「两次续租
       机会都在过期之前」：即使某一轮续租因 DB 抖动/调度延迟被推迟到接近一个周期，
       下一轮仍能在租约过期前落地。用倍数表达而不是 ``lease > task_time_limit``，
       因为后者会把正常的长任务配置判为非法。
    2. ``tool_timeout < task_time_limit``：工具超时必须先于 run 级上限触发，否则
       局部降级永远来不及发生。
    3. ``task_time_limit < visibility_timeout``：broker 的未 ACK 可见性时间必须大于
       最长可能执行时间，否则任务还在跑、消息已被重新投递 -> 并发执行同一 run。
    4. ``claim_ttl >= tool_timeout + margin``：side-effect 认领租约必须覆盖受保护
       副作用的执行上界，否则执行期间认领过期、第二个执行者重复发起同一笔写。
       （与 ``runtime.side_effects.min_claim_ttl_seconds`` 同源。）
    5. ``thread_lock_ttl > task_time_limit + margin``：锁 TTL 必须覆盖任务执行，
       否则任务还在跑、同 thread 的另一个 worker 就能进入同一条 state lineage。
       ``lock_enabled=False`` 时不适用。

    刻意**不**作为致命约束的（只影响恢复延迟或运维偏好）
    --------------------------------------------------
    - ``lease > task_time_limit``：有正常续租时这不是正确性问题；只在无法续租的
      部署（例如心跳被禁用）下才成为问题，因此记为建议而不是拒绝启动。
    - ``reconcile_interval`` 与 ``lease`` 的相对大小：只改变滞留 run 的检测延迟。
    """
    problems: list[str] = []

    def _off(value: float) -> bool:
        """<= 0 视为「显式关闭」，该维度不参与校验。"""
        return value <= 0

    if not _off(lease_seconds) and not _off(heartbeat_seconds):
        if heartbeat_seconds >= lease_seconds:
            problems.append(
                f"AGENT_RUN_HEARTBEAT_SECONDS({heartbeat_seconds}) 必须小于 "
                f"AGENT_RUN_LEASE_SECONDS({lease_seconds}) —— 否则续租永远追不上过期，"
                "lease 会恒定失效，任何执行都会被判为失去所有权"
            )
        elif lease_seconds < heartbeat_seconds * 2:
            problems.append(
                f"AGENT_RUN_LEASE_SECONDS({lease_seconds}) 至少要是 "
                f"AGENT_RUN_HEARTBEAT_SECONDS({heartbeat_seconds}) 的两倍 —— 周期续租需要"
                "「两次机会都在过期之前」才能证明长执行不会因单次抖动丢租约"
            )

    if (
        task_time_limit_enabled
        and not _off(tool_timeout_seconds)
        and not _off(task_time_limit_seconds)
        and tool_timeout_seconds >= task_time_limit_seconds
    ):
        problems.append(
            f"TOOL_EXECUTION_TIMEOUT_SECONDS({tool_timeout_seconds}) 必须小于 "
            f"AGENT_RUN_TASK_TIME_LIMIT({task_time_limit_seconds})"
        )

    if (
        task_time_limit_enabled
        and not _off(task_time_limit_seconds)
        and not _off(visibility_timeout_seconds)
        and visibility_timeout_seconds <= task_time_limit_seconds
    ):
        problems.append(
            f"AGENT_RUN_VISIBILITY_TIMEOUT({visibility_timeout_seconds}) 必须大于 "
            f"AGENT_RUN_TASK_TIME_LIMIT({task_time_limit_seconds}) —— broker 的未 ACK "
            "可见性时间必须覆盖最长可能执行时间，否则任务仍在执行时消息已被重新投递，"
            "同一 run 被并发执行"
        )

    effective_claim_ttl = (
        float(claim_ttl_seconds)
        if claim_ttl_seconds is not None
        else _default_side_effect_claim_ttl()
    )
    if not _off(effective_claim_ttl) and not _off(tool_timeout_seconds):
        margin = _claim_ttl_margin_seconds()
        floor = tool_timeout_seconds + margin
        if effective_claim_ttl < floor:
            problems.append(
                f"side-effect claim TTL({effective_claim_ttl}) 必须不小于 "
                f"TOOL_EXECUTION_TIMEOUT_SECONDS({tool_timeout_seconds}) + margin"
                f"({margin}) = {floor} —— 执行期间认领过期会让第二个"
                "执行者拿到执行权并重复发起同一笔写操作"
            )

    if (
        lock_enabled
        and task_time_limit_enabled
        and not _off(thread_lock_ttl_seconds)
        and not _off(task_time_limit_seconds)
        and thread_lock_ttl_seconds <= task_time_limit_seconds + 30.0
    ):
        problems.append(
            f"AGENT_RUN_THREAD_LOCK_TTL_SECONDS({thread_lock_ttl_seconds}) 必须大于 "
            f"AGENT_RUN_TASK_TIME_LIMIT({task_time_limit_seconds}) + safety margin(30.0)"
            " —— 否则任务仍在执行时锁已过期，同 thread 的另一个 worker 可以进入"
            "同一条 state lineage"
        )

    return problems


def timeout_invariant_recommendations(
    *,
    lease_seconds: float = AGENT_RUN_LEASE_SECONDS,
    task_time_limit_seconds: float = float(AGENT_RUN_TASK_TIME_LIMIT),
    heartbeat_enabled: bool = True,
) -> list[str]:
    """**非致命**建议（只影响恢复延迟或运维偏好，不拒绝启动）。

    与 :func:`validate_timeout_invariants` 分开，是为了不把「偏好」伪装成「正确性」。
    """
    out: list[str] = []
    if not heartbeat_enabled and 0 < lease_seconds <= task_time_limit_seconds:
        out.append(
            f"建议：心跳未启用时 AGENT_RUN_LEASE_SECONDS({lease_seconds}) 应大于 "
            f"AGENT_RUN_TASK_TIME_LIMIT({task_time_limit_seconds})，否则长任务会在执行途中"
            "被判为失去所有权并被 fencing 取消"
        )
    return out


def validate_tool_settings(
    *,
    tool_timeout_seconds: float = TOOL_EXECUTION_TIMEOUT_SECONDS,
    task_time_limit_seconds: float = float(AGENT_RUN_TASK_TIME_LIMIT),
) -> list[str]:
    """校验工具执行配置（纯函数，无 IO），返回错误信息列表（空 = 通过）。

    只做**关系校验**，不检查单个值的合法性 —— 负数在下面被单独当作「显式关闭」。

    唯一的不变量：工具超时必须**小于** run 的 task time limit。否则工具超时形同
    虚设 —— 局部降级永远不会先发生，每次都是等 run 被整体掐断，那等于没有超时。
    """
    if tool_timeout_seconds <= 0:
        return []
    if task_time_limit_seconds <= 0:
        return []
    if tool_timeout_seconds >= task_time_limit_seconds:
        return [
            f"TOOL_EXECUTION_TIMEOUT_SECONDS({tool_timeout_seconds}) 必须小于 "
            f"AGENT_RUN_TASK_TIME_LIMIT({task_time_limit_seconds}) —— 否则工具超时会先被 "
            "run 级 time limit 抢先，局部降级退化为整体失败；"
            "如确实不需要工具超时，请显式设为 0"
        ]
    return []


def validate_hitl_settings(
    *,
    enabled: bool = HITL_ENABLED,
    high_risk_tools: str = HITL_HIGH_RISK_TOOLS,
    medium_risk_tools: str = HITL_MEDIUM_RISK_TOOLS,
    high_amount_threshold: float = HITL_HIGH_AMOUNT_THRESHOLD,
    ttl_seconds: float = HITL_APPROVAL_TTL_SECONDS,
) -> list[str]:
    """校验 human-in-the-loop 配置（纯函数，无 IO），返回错误信息列表（空 = 通过）。

    为什么 HITL 需要像 MCP 一样的 fail-closed 校验
    ---------------------------------------------
    ``HITL_ENABLED=true`` 只表示「打开审批治理」。若**没有任何规则能命中 HIGH**
    （``HITL_HIGH_RISK_TOOLS`` 为空 **且** 金额阈值 <= 0），那么
    ``core.hitl.risk.classify_risk`` 的判定链会一路落到默认值，所有工具都判为
    LOW，审批闸门**什么都不拦**，而且没有任何日志、指标或告警。

    那是一次典型的「声称与事实不符」：配置里写着审批已启用，运行时却没有审批。
    且它发生在最常见的运维事故形态下——开了开关、忘了配规则。因此这里选择
    **拒绝启动**：治理边界要么真的生效，要么就别声称生效。

    与 ``classify_risk`` 的 side-effect 兜底是两层互补的防线，不要互相替代：
      - 本校验回答「你的配置**根本不可能**产生任何 HIGH 判定」；
      - ``classify_risk`` 的 ``side_effect`` 兜底回答「个别工具**没有被任何规则
        覆盖**」。

    只在显式开启时校验：默认 ``false`` 时治理边界本就不在承诺范围内，
    不该因为「没配规则」而阻止服务启动。
    """
    if not enabled:
        return []

    errors: list[str] = []
    has_high_names = any(t.strip() for t in (high_risk_tools or "").split(","))
    has_amount_rule = high_amount_threshold > 0
    if not has_high_names and not has_amount_rule:
        errors.append(
            "HITL_ENABLED=true 但没有任何规则会产生 HIGH 风险判定"
            "（HITL_HIGH_RISK_TOOLS 为空 且 HITL_HIGH_AMOUNT_THRESHOLD<=0）"
            "—— 实际后果：执行期兜底会把**每个**声明了 side_effect=True 且未声明"
            "risk_level 的工具判为 HIGH（fail-closed，只读工具不受影响），"
            "也就是说治理面是「所有写操作」而非你预期的白名单，且在工具真正被调用前"
            "完全不可见。请显式配置 HITL_HIGH_RISK_TOOLS（逗号分隔的工具名）"
            "或设置金额阈值，把治理面写进配置而不是靠兜底；"
            "若确实不需要该治理，请显式设置 HITL_ENABLED=false。"
        )
    if ttl_seconds <= 0:
        errors.append(
            f"HITL_APPROVAL_TTL_SECONDS 必须为正数（当前 {ttl_seconds}）"
            "—— 否则审批会立即过期且按拒绝处理，治理变成「一律拒绝」"
        )
    if medium_risk_tools.strip() and high_risk_tools.strip() == medium_risk_tools.strip():
        errors.append(
            "HITL_HIGH_RISK_TOOLS 与 HITL_MEDIUM_RISK_TOOLS 完全相同"
            " —— 同一批工具既要求审批又声明为免审批，配置自相矛盾"
        )
    return errors


def validate_session_encryption_settings(
    *,
    encryption_key: str = SESSION_ENCRYPTION_KEY,
    plaintext_compat: str = SESSION_ENCRYPTION_PLAINTEXT_COMPAT,
) -> list[str]:
    """校验会话加密的**格式判别**配置（纯函数，无 IO），返回错误信息列表。

    为什么这里必须 fail closed
    --------------------------
    ``SESSION_ENCRYPTION_PLAINTEXT_COMPAT`` 决定"一条既不是带标记密文、也不是旧版
    Fernet token 的记录"该怎么判。这个判定的两种可能结果安全方向完全相反：

      * ``legacy``  -> 当作历史明文**接受**（数据完整性无法验证，见下方边界）；
      * ``reject``  -> 拒绝加载。

    若把它静默兜底成 ``legacy``，一个拼写错误的配置（例如 ``REJECT`` 写成 ``rejct``、
    或写成 ``strict``）就会**无声地**把本该 fail-closed 的部署降级成"接受任意非密文
    记录"，而运维界面上看不出任何异常——正是本仓反复要防的「声称与事实不符」。

    因此：取值不在枚举内就是配置错误，不猜、不兜底。

    边界（不因本函数通过而声称成立）
    ------------------------------
    即便 ``reject`` 全开，它也**不能**证明"没有攻击者能替换会话记录"：攻击者若能写
    Redis / 会话文件，同样能改这一条配置所依赖的存储内容。``reject`` 消除的是
    "密文损坏被静默降级成明文"这一类**降级攻击**，不是对存储写权限的整体防护。
    """
    errors: list[str] = []
    if plaintext_compat not in SESSION_ENCRYPTION_PLAINTEXT_COMPAT_VALUES:
        errors.append(
            f"SESSION_ENCRYPTION_PLAINTEXT_COMPAT 取值非法：{plaintext_compat!r}"
            f" —— 只允许 {'/'.join(SESSION_ENCRYPTION_PLAINTEXT_COMPAT_VALUES)}。"
            " 不做兜底：静默降级成 legacy 会让本应 fail-closed 的部署无声地"
            "接受任意非密文记录。"
        )
    elif not encryption_key and plaintext_compat == SESSION_ENCRYPTION_PLAINTEXT_COMPAT_REJECT:
        # 与 ``validate_hitl_settings`` 同一条原则：配置要么真的产生该治理效果，
        # 要么就别声称。未配置密钥时 ``_open_record`` 在 ``if not key`` 处直接返回，
        # 根本不读这一项 —— 也就是说 reject **完全不起作用**，却让人以为已开启严格模式。
        errors.append(
            "SESSION_ENCRYPTION_PLAINTEXT_COMPAT=reject 但未配置 SESSION_ENCRYPTION_KEY"
            " —— 未配置密钥时本项**完全不生效**（所有记录都是明文），"
            "配置声称的严格模式是假的。要么配置 SESSION_ENCRYPTION_KEY，"
            "要么改回 legacy。"
        )
    return errors


# ===== P0-3: 生产环境关键配置启动校验 =====
def validate_required_config():
    """生产环境启动时校验关键配置项非空非占位符"""
    # ---- 超时/租约交叉不变量（**先于** DEV_MODE 早退）----
    # 与 HITL 同理：这些关系错了会在运行时产生**不确定的重复执行**（重复投递、
    # 重复写、锁丢失），与「是不是生产」无关。开发机上放过它意味着集成测试拿到
    # 虚假的安全性 —— 而这类问题恰恰只在并发下偶发，事后极难归因。
    # 不满足时拒绝启动：宁可启动失败，也不要一个会静默重复写进程的配置。
    timeout_errors = validate_timeout_invariants(
        lease_seconds=AGENT_RUN_LEASE_SECONDS,
        heartbeat_seconds=AGENT_RUN_HEARTBEAT_SECONDS,
        task_time_limit_seconds=float(AGENT_RUN_TASK_TIME_LIMIT),
        visibility_timeout_seconds=float(AGENT_RUN_VISIBILITY_TIMEOUT),
        tool_timeout_seconds=TOOL_EXECUTION_TIMEOUT_SECONDS,
        thread_lock_ttl_seconds=AGENT_RUN_THREAD_LOCK_TTL_SECONDS,
        lock_enabled=AGENT_RUN_THREAD_LOCK_ENABLED,
    )
    if timeout_errors:
        for err in timeout_errors:
            print(f"🚨 超时不变量校验失败: {err}", file=sys.stderr)
        raise ConfigurationError(
            f"超时/租约配置不满足正确性约束（{len(timeout_errors)} 项）："
            "这些关系会在运行时产生不确定的重复执行或锁丢失，拒绝启动"
        )
    for rec in timeout_invariant_recommendations(
        lease_seconds=AGENT_RUN_LEASE_SECONDS,
        task_time_limit_seconds=float(AGENT_RUN_TASK_TIME_LIMIT),
    ):
        print(f"⚠️  {rec}", file=sys.stderr)

    # ---- HITL 治理有效性（**先于** DEV_MODE 早退）----
    # 它与「是不是生产」无关：``HITL_ENABLED=true`` 却配不出任何 HIGH 判定，
    # 在开发机上同样会让集成测试与本地验收拿到虚假的安全感。因此这里刻意放在
    # DEV_MODE 早退之前 —— 治理要么真的生效，要么拒绝声称生效。
    hitl_errors = validate_hitl_settings(
        enabled=HITL_ENABLED,
        high_risk_tools=HITL_HIGH_RISK_TOOLS,
        medium_risk_tools=HITL_MEDIUM_RISK_TOOLS,
        high_amount_threshold=HITL_HIGH_AMOUNT_THRESHOLD,
        ttl_seconds=HITL_APPROVAL_TTL_SECONDS,
    )
    if hitl_errors:
        for err in hitl_errors:
            print(f"🚨 HITL 配置校验失败: {err}", file=sys.stderr)
        raise ConfigurationError(
            f"HITL 治理配置无效（{len(hitl_errors)} 项）：审批开关已开启但配置无法产生任何治理效果"
        )

    # ---- 会话加密格式判别（**先于** DEV_MODE 早退，同上）----
    # ``SESSION_ENCRYPTION_PLAINTEXT_COMPAT`` 取值非法时，两种可能结果安全方向相反
    # （legacy=接受明文 / reject=拒绝），静默兜底成 legacy 会让部署无声地失去
    # fail-closed 语义。取值非法就是配置错误，不猜。
    session_enc_errors = validate_session_encryption_settings(
        encryption_key=SESSION_ENCRYPTION_KEY,
        plaintext_compat=SESSION_ENCRYPTION_PLAINTEXT_COMPAT,
    )
    if session_enc_errors:
        for err in session_enc_errors:
            print(f"🚨 会话加密配置校验失败: {err}", file=sys.stderr)
        raise ConfigurationError(
            f"会话加密配置无效（{len(session_enc_errors)} 项）：明文兼容策略取值不在允许枚举内"
        )

    # v4.3 安全加固：生产环境禁止 DEV_MODE
    if _DEV_MODE:
        # 检测是否为生产环境（通过多个信号判断）
        _is_production = (
            os.getenv("APP_MODE", "").lower() == "prod"
            or os.getenv("LOG_FORMAT", "").lower() == "json"
            or os.getenv("ENVIRONMENT", "").lower() == "production"
        )
        if _is_production:
            raise ConfigurationError(
                "🚨 安全错误: 生产环境(APP_MODE=prod/LOG_FORMAT=json/ENVIRONMENT=production)下禁止 DEV_MODE=true！"
                "请设置 DEV_MODE=false 后重启。"
            )
        return  # 非生产环境的开发模式跳过后续校验

    _PLACEHOLDER_PREFIXES = (
        "your-",
        "change-me",
        "change_me",
        "sk-placeholder",
        "sk-xxx",
        "sk-your",
        "sk-test-placeholder",
        "sk-tnwwg",  # 匹配已知旧占位符
    )
    errors = []

    # LLM API Key
    if not OPENAI_API_KEY or any(
        OPENAI_API_KEY.lower().startswith(p) for p in _PLACEHOLDER_PREFIXES
    ):
        errors.append("OPENAI_API_KEY 未配置或使用占位符")

    # ---- 以下均为 **API 进程专属**：worker 不终结 HTTP 请求、不签发/校验用户凭据，
    # 因此不应该因为缺少这些配置而无法启动（见 SERVICE_ROLE / IS_WORKER_PROCESS）。
    if not IS_WORKER_PROCESS:
        # JWT Secret（v5.0: 最小 32 字符，防止弱密钥）
        if not JWT_SECRET or any(
            p in JWT_SECRET.lower() for p in ("change-me", "change_me", "your-", "dev-")
        ):
            errors.append("JWT_SECRET 未配置或使用默认值/弱密钥")
        elif len(JWT_SECRET) < 32:
            errors.append(
                f"JWT_SECRET 长度不足（{len(JWT_SECRET)} < 32），请使用至少 32 字符的随机密钥"
            )

        # Session Token Secret
        if not SESSION_TOKEN_SECRET or any(
            p in SESSION_TOKEN_SECRET.lower() for p in ("change-me", "change_me", "your-", "dev-")
        ):
            errors.append("SESSION_TOKEN_SECRET 未配置或使用默认值/弱密钥")
        elif len(SESSION_TOKEN_SECRET) < 32:
            errors.append(
                f"SESSION_TOKEN_SECRET 长度不足（{len(SESSION_TOKEN_SECRET)} < 32），"
                "请使用至少 32 字符的随机密钥"
            )

        if not _DEV_MODE and "*" in CORS_ORIGINS:
            errors.append("Production CORS_ORIGINS must not contain wildcard *")
        if not _DEV_MODE and not CORS_ORIGINS:
            errors.append(
                "Production CORS_ORIGINS is empty — frontend cross-origin requests will "
                "fail. Set CORS_ORIGINS or ALLOWED_ORIGINS"
            )

    if not _DEV_MODE:
        errors.extend(validate_app_database_dsn(dev_mode=_DEV_MODE, database_url=DATABASE_URL))
    elif not _DEV_MODE and not DATABASE_URL.startswith(("postgresql://", "postgres://")):
        # 应用库必须是 PostgreSQL：非 PostgreSQL 方言（SQLite/MySQL）没有行级锁，
        # side-effect ledger 的接管路径无法排除并发重复执行（Issue #13）。注意
        # LANGGRAPH_CHECKPOINT_DATABASE_URL 是**独立**设置，所以
        # 「PG checkpoint + SQLite 应用库」的组合会骗过只检查 checkpoint 的校验；
        # 这里对**应用库**单独 fail-fast，避免带病启动。
        errors.append(
            "Production DATABASE_URL must be PostgreSQL (got a non-PostgreSQL DSN): "
            "the side-effect ledger's takeover path requires row-level locking; "
            "SQLite/MySQL cannot provide it (see Issue #13)"
        )

    # LangGraph checkpoint：生产禁止进程内 MemorySaver（多 worker/多副本不共享、
    # 重启即丢失），且必须能解析出可用的 PostgreSQL DSN。
    if not _DEV_MODE:
        errors.extend(
            validate_checkpoint_settings(
                LANGGRAPH_CHECKPOINT_BACKEND,
                explicit_url=LANGGRAPH_CHECKPOINT_DATABASE_URL,
                database_url=DATABASE_URL,
                dev_mode=_DEV_MODE,
            )
        )

    # MCP：生产开启时必须给出可用的 server allowlist。MCP 默认关闭，所以这里只在
    # 显式开启时校验；fail closed —— 配错了就拒绝启动，而不是让服务带着「以为 MCP
    # 已生效、实际一个工具都没注册」的状态跑起来。
    errors.extend(
        validate_mcp_settings(
            enabled=MCP_ENABLED,
            servers=MCP_SERVERS,
            max_payload_bytes=MCP_MAX_PAYLOAD_BYTES,
        )
    )

    # 工具执行超时必须真的早于 run 级 time limit 触发，否则局部降级形同虚设。
    errors.extend(
        validate_tool_settings(
            tool_timeout_seconds=TOOL_EXECUTION_TIMEOUT_SECONDS,
            task_time_limit_seconds=float(AGENT_RUN_TASK_TIME_LIMIT),
        )
    )

    # P0-2 / P0-6: 生产 session + dispatch + gunicorn 多 worker + lock TTL（纯函数）
    errors.extend(
        validate_distributed_runtime_settings(
            dev_mode=_DEV_MODE,
            session_backend=SESSION_STORAGE_BACKEND,
            checkpoint_backend=LANGGRAPH_CHECKPOINT_BACKEND,
            gunicorn_workers=_int_env("GUNICORN_WORKERS", 0),
            lock_enabled=AGENT_RUN_THREAD_LOCK_ENABLED,
            lock_backend=AGENT_RUN_THREAD_LOCK_BACKEND,
            agent_run_dispatch=AGENT_RUN_DISPATCH,
            lock_ttl_seconds=AGENT_RUN_THREAD_LOCK_TTL_SECONDS,
            task_time_limit_seconds=float(AGENT_RUN_TASK_TIME_LIMIT),
        )
    )

    if errors:
        for err in errors:
            print(f"🚨 配置校验失败: {err}", file=sys.stderr)
        raise ConfigurationError(
            f"生产环境启动失败：{len(errors)} 项关键配置缺失，请检查 .env 文件"
        )

    # Non-fatal warnings for missing optional-but-recommended config
    warnings: list[str] = []
    if not _DEV_MODE and not RAG_PERSIST_DIRECTORY:
        warnings.append("RAG_PERSIST_DIRECTORY not set, vector DB will run in-memory")
    if not _DEV_MODE and not ALERT_WEBHOOKS and not SMTP_HOST:
        warnings.append("No alert notification channels configured")
    if not _DEV_MODE and ERP_MODE == "mock":
        warnings.append("ERP_MODE=mock: using fake ERP data, set to 'real' for production")
    if not _DEV_MODE and not SESSION_ENCRYPTION_KEY:
        warnings.append("SESSION_ENCRYPTION_KEY not set: session data stored in plaintext")
    # v6.3: 当 EMBEDDING_API_KEY 为空（即从 OPENAI_API_KEY 回退）时发出警告
    _embedding_raw = os.getenv("EMBEDDING_API_KEY", "")
    if not _DEV_MODE and not _embedding_raw:
        warnings.append(
            "EMBEDDING_API_KEY not set, reusing OPENAI_API_KEY for embedding service — configure a dedicated key for production"
        )
    if not _DEV_MODE and AGENT_RUN_DISPATCH == "inline":
        warnings.append(
            "AGENT_RUN_DISPATCH=inline in production: runs execute inside the API "
            "process (no worker decoupling). Use 'celery' for production."
        )
    if not _DEV_MODE and AGENT_RUN_THREAD_LOCK_BACKEND == "memory":
        warnings.append(
            "AGENT_RUN_THREAD_LOCK_BACKEND=memory in production: thread lock is "
            "process-local, not shared across workers/replicas. Use 'redis'."
        )

    for w in warnings:
        logging.getLogger("config").warning(f"[config] {w}")


validate_required_config()


# ===== v5.0: FeatureFlags 集中管理 =====
class FeatureFlags:
    """功能开关集中管理，所有开关统一从此处读取"""

    # 多模态
    MULTIMODAL_ENABLED = MULTIMODAL_ENABLED
    # A/B 测试
    AB_TEST_ENABLED = AB_TEST_ENABLED
    # SSE 流式输出
    SSE_ENABLED = SSE_ENABLED
    # 自动重试/模式升级
    EVAL_RETRY_ENABLED = EVAL_RETRY_ENABLED
    MODE_UPGRADE_ENABLED = MODE_UPGRADE_ENABLED
    # Function Calling
    TOOL_MAX_ROUNDS = TOOL_MAX_ROUNDS
    TOOL_RESULT_OPTIMIZATION_ENABLED = TOOL_RESULT_OPTIMIZATION_ENABLED
    TOOL_RESULT_MAX_TOKENS = TOOL_RESULT_MAX_TOKENS
    TOOL_RESULT_MAX_ITEMS = TOOL_RESULT_MAX_ITEMS
    TOOL_RESULT_PRESERVE_RECENT = TOOL_RESULT_PRESERVE_RECENT
    TOOL_RESULT_OFFLOAD_ENABLED = TOOL_RESULT_OFFLOAD_ENABLED
    TOOL_RESULT_OFFLOAD_MIN_TOKENS = TOOL_RESULT_OFFLOAD_MIN_TOKENS
    TOOL_RESULT_STORE_TTL_SECONDS = TOOL_RESULT_STORE_TTL_SECONDS
    TOOL_RESULT_SEMANTIC_SUMMARY_ENABLED = TOOL_RESULT_SEMANTIC_SUMMARY_ENABLED
    TOOL_RESULT_CACHE_ENABLED = TOOL_RESULT_CACHE_ENABLED
    # ReAct 推理
    REACT_COMPLEXITY_THRESHOLD = REACT_COMPLEXITY_THRESHOLD

    @classmethod
    def get_all(cls) -> dict[str, bool | int]:
        """返回所有功能开关的当前状态（用于健康检查/调试）"""
        return {
            "multimodal": cls.MULTIMODAL_ENABLED,
            "ab_test": cls.AB_TEST_ENABLED,
            "sse": cls.SSE_ENABLED,
            "eval_retry": cls.EVAL_RETRY_ENABLED,
            "mode_upgrade": cls.MODE_UPGRADE_ENABLED,
            "tool_max_rounds": cls.TOOL_MAX_ROUNDS,
            "tool_result_optimization": cls.TOOL_RESULT_OPTIMIZATION_ENABLED,
            "tool_result_max_tokens": cls.TOOL_RESULT_MAX_TOKENS,
            "tool_result_max_items": cls.TOOL_RESULT_MAX_ITEMS,
            "tool_result_preserve_recent": cls.TOOL_RESULT_PRESERVE_RECENT,
            "tool_result_offload": cls.TOOL_RESULT_OFFLOAD_ENABLED,
            "tool_result_offload_min_tokens": cls.TOOL_RESULT_OFFLOAD_MIN_TOKENS,
            "tool_result_store_ttl_seconds": cls.TOOL_RESULT_STORE_TTL_SECONDS,
            "tool_result_semantic_summary": cls.TOOL_RESULT_SEMANTIC_SUMMARY_ENABLED,
            "tool_result_cache": cls.TOOL_RESULT_CACHE_ENABLED,
            "react_complexity_threshold": cls.REACT_COMPLEXITY_THRESHOLD,
        }
