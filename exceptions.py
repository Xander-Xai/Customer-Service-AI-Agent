"""Custom exception hierarchy for the customer service AI agent system.

All application-specific exceptions inherit from AppError.
Use these instead of bare Exception for typed error handling.
"""


class AppError(Exception):
    """Base exception for all application errors."""

    def __init__(self, message: str = "", code: str = ""):
        super().__init__(message)
        self.code = code


class AuthError(AppError):
    """Authentication or authorization failure."""

    def __init__(self, message: str = "认证失败", code: str = "AUTH_ERROR"):
        super().__init__(message, code)


class RateLimitError(AppError):
    """Rate limit exceeded."""

    def __init__(self, message: str = "请求过于频繁", code: str = "RATE_LIMIT"):
        super().__init__(message, code)


class ValidationError(AppError):
    """Input validation failure."""

    def __init__(self, message: str = "输入校验失败", code: str = "VALIDATION_ERROR"):
        super().__init__(message, code)


class LLMError(AppError):
    """LLM service error."""

    def __init__(self, message: str = "LLM 服务异常", code: str = "LLM_ERROR"):
        super().__init__(message, code)


class LLMTimeoutError(LLMError):
    """LLM request timed out."""

    def __init__(self, message: str = "LLM 请求超时", code: str = "LLM_TIMEOUT"):
        super().__init__(message, code)


class LLMRateLimitError(LLMError):
    """LLM provider rate limit."""

    def __init__(self, message: str = "LLM 限流", code: str = "LLM_RATE_LIMIT"):
        super().__init__(message, code)


class KnowledgeError(AppError):
    """RAG/knowledge base error."""

    def __init__(self, message: str = "知识库异常", code: str = "KNOWLEDGE_ERROR"):
        super().__init__(message, code)


class SessionError(AppError):
    """Session management error."""

    def __init__(self, message: str = "会话管理异常", code: str = "SESSION_ERROR"):
        super().__init__(message, code)


class ERPError(AppError):
    """ERP system integration error."""

    def __init__(self, message: str = "ERP 系统异常", code: str = "ERP_ERROR"):
        super().__init__(message, code)
