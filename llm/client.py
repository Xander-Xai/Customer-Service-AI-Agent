"""
OpenAI 兼容 LLM 异步客户端

从 core/monitoring.py 迁移，职责独立：
- CustomResponse: LLM 响应包装（支持 Function Calling tool_calls）
- OpenAICompatibleClient: HTTP 客户端（指数退避 + 熔断器 + 连接池）

v3.4: 熔断器 async 调用 + 实例级连接池隔离
v3.5: Function Calling 支持 + 不支持 tools 时自动降级
v3.7: 不向调用方泄露内部错误细节
v4.1: 多模态消息格式 + max_tokens 配置
v4.2: 真流式调用（SSE 逐 chunk）
"""
import os
import json
import random
import asyncio
import httpx
from typing import Optional

from logger import get_logger, get_trace_id
from config import (
    RETRY_MAX_ATTEMPTS, RETRY_BASE_DELAY, HTTP_TIMEOUT, HTTP_HEADERS,
    HTTPX_MAX_CONNECTIONS, HTTPX_KEEPALIVE_CONNECTIONS,
)

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage

logger = get_logger("llm_client")


class CustomResponse:
    """LLM 响应包装（v3.5: 支持 Function Calling tool_calls）"""
    def __init__(self, content: str, tool_calls: list = None):
        self.content = content
        self.tool_calls = tool_calls  # [{"id", "name", "arguments"}] or None


class OpenAICompatibleClient:
    """OpenAI 兼容异步客户端（指数退避 + 熔断器 + 连接池）"""
    _client_pools: dict = {}  # 按 base_url 隔离连接池
    _pool_lock: asyncio.Lock = asyncio.Lock()

    def __init__(self, api_key: str, base_url: str, model: str,
                 circuit_breaker=None):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = HTTP_TIMEOUT
        self.max_retries = RETRY_MAX_ATTEMPTS
        self.base_delay = RETRY_BASE_DELAY
        self.headers = HTTP_HEADERS.copy()
        self.headers["Authorization"] = f"Bearer {api_key}"
        self.circuit_breaker = circuit_breaker

    @staticmethod
    def _format_messages(messages) -> list:
        """统一消息格式化（支持 ToolMessage、AIMessage.tool_calls、多模态）"""
        formatted = []
        for msg in messages:
            if hasattr(msg, 'type') and msg.type == 'tool':
                formatted.append({"role": "tool", "content": msg.content, "tool_call_id": getattr(msg, 'tool_call_id', '')})
            elif hasattr(msg, 'tool_calls') and msg.tool_calls:
                formatted.append({"role": "assistant", "content": msg.content or "", "tool_calls": msg.tool_calls})
            elif isinstance(msg, SystemMessage):
                formatted.append({"role": "system", "content": msg.content})
            elif isinstance(msg, HumanMessage):
                if isinstance(msg.content, list):
                    formatted.append({"role": "user", "content": msg.content})
                else:
                    formatted.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AIMessage):
                formatted.append({"role": "assistant", "content": msg.content})
            else:
                formatted.append({"role": "user", "content": str(msg.content)})
        return formatted

    async def _get_async_client(self) -> httpx.AsyncClient:
        """连接池并发安全（pool lock 保护）"""
        async with OpenAICompatibleClient._pool_lock:
            pool_key = self.base_url
            client = OpenAICompatibleClient._client_pools.get(pool_key)
            if client is None or client.is_closed:
                client = httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout),
                    limits=httpx.Limits(
                        max_connections=HTTPX_MAX_CONNECTIONS,
                        max_keepalive_connections=HTTPX_KEEPALIVE_CONNECTIONS,
                    ),
                )
                OpenAICompatibleClient._client_pools[pool_key] = client
            return client

    @classmethod
    async def close_all_clients(cls):
        """关闭所有连接池中的客户端"""
        for client in cls._client_pools.values():
            if not client.is_closed:
                await client.aclose()
        cls._client_pools.clear()

    async def async_invoke(self, messages, timeout: Optional[float] = None,
                           tools: Optional[list] = None):
        """异步调用（支持 Function Calling，不支持 tools 时自动降级）"""
        if self.circuit_breaker and not await self.circuit_breaker.should_allow():
            raise Exception("CircuitBreaker OPEN: LLM 调用已熔断，走降级路径")

        payload = {"model": self.model, "messages": self._format_messages(messages)}
        if "max_tokens" not in payload:
            payload["max_tokens"] = int(os.environ.get("LLM_MAX_TOKENS", "4096"))
        if tools:
            payload["tools"] = tools
        client = await self._get_async_client()
        call_timeout = httpx.Timeout(timeout or self.timeout)

        last_error = None
        for attempt in range(self.max_retries):
            try:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload, headers=self.headers, timeout=call_timeout,
                )
                resp.raise_for_status()
                result = resp.json()
                if "choices" in result and result["choices"]:
                    message = result["choices"][0].get("message", {})
                    content = message.get("content", "") or ""
                    tool_calls_raw = message.get("tool_calls")
                    parsed_tool_calls = None
                    if tool_calls_raw:
                        parsed_tool_calls = []
                        for tc in tool_calls_raw:
                            fn = tc.get("function", {})
                            parsed_tool_calls.append({
                                "id": tc.get("id", ""),
                                "name": fn.get("name", ""),
                                "arguments": fn.get("arguments", "{}"),
                            })
                    if self.circuit_breaker:
                        await self.circuit_breaker.record_success()
                    return CustomResponse(content, parsed_tool_calls)
                return CustomResponse("API response format error")
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                last_error = e
                if isinstance(e, httpx.HTTPStatusError) and tools and e.response.status_code in (400, 422):
                    logger.warning(f"[LLM] [{get_trace_id()}] 模型不支持 tools 参数，降级为普通调用: {e}")
                    del payload["tools"]
                    tools = None
                    continue
                if attempt == self.max_retries - 1:
                    break
                max_delay = 10.0
                delay = min(self.base_delay * (2 ** attempt) + random.uniform(0, 0.3), max_delay)
                logger.warning(f"[LLM-async] [{get_trace_id()}] retry {attempt+1}/{self.max_retries}: {e}, wait {delay:.1f}s")
                await asyncio.sleep(delay)

        if self.circuit_breaker:
            await self.circuit_breaker.record_failure()
        raise Exception(f"LLM API 调用失败（已重试 {self.max_retries} 次），请稍后重试")

    async def async_invoke_raw(self, messages: list, timeout: Optional[float] = None):
        """使用预格式化的消息列表直接调用（适用于多模态等需要原始 content 格式的场景）
        messages: 已格式化的 OpenAI 格式消息列表 [{"role": "user", "content": [...]}]
        跳过 _format_messages，直接传递。
        """
        if self.circuit_breaker and not await self.circuit_breaker.should_allow():
            raise Exception("CircuitBreaker OPEN: LLM 调用已熔断")

        payload = {"model": self.model, "messages": messages}
        if "max_tokens" not in payload:
            payload["max_tokens"] = int(os.environ.get("LLM_MAX_TOKENS", "4096"))
        client = await self._get_async_client()
        call_timeout = httpx.Timeout(timeout or self.timeout)

        for attempt in range(self.max_retries):
            try:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload, headers=self.headers, timeout=call_timeout,
                )
                resp.raise_for_status()
                result = resp.json()
                if self.circuit_breaker:
                    await self.circuit_breaker.record_success()
                if "choices" in result and result["choices"]:
                    message = result["choices"][0].get("message", {})
                    return CustomResponse(message.get("content", "") or "")
                return CustomResponse("API response format error")
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                if attempt == self.max_retries - 1:
                    break
                delay = min(self.base_delay * (2 ** attempt) + random.uniform(0, 0.3), 10.0)
                logger.warning(f"[LLM-raw] retry {attempt+1}/{self.max_retries}: {e}, wait {delay:.1f}s")
                await asyncio.sleep(delay)

        if self.circuit_breaker:
            await self.circuit_breaker.record_failure()
        raise Exception(f"LLM API 调用失败（已重试 {self.max_retries} 次），请稍后重试")

    async def async_invoke_stream(self, messages, timeout: Optional[float] = None):
        """流式调用（SSE 逐 chunk 接收）"""
        if self.circuit_breaker and not await self.circuit_breaker.should_allow():
            raise Exception("CircuitBreaker OPEN: LLM 流式调用已熔断")

        payload = {
            "model": self.model,
            "messages": self._format_messages(messages),
            "stream": True,
            "max_tokens": int(os.environ.get("LLM_MAX_TOKENS", "4096")),
        }
        client = await self._get_async_client()
        call_timeout = httpx.Timeout(timeout or self.timeout)

        last_error = None
        for attempt in range(self.max_retries):
            try:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload, headers=self.headers, timeout=call_timeout,
                ) as resp:
                    resp.raise_for_status()
                    if self.circuit_breaker:
                        await self.circuit_breaker.record_success()

                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                return
                            try:
                                chunk = json.loads(data)
                                choices = chunk.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        yield content
                            except json.JSONDecodeError:
                                continue
                return
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                last_error = e
                if attempt == self.max_retries - 1:
                    break
                max_delay = 10.0
                delay = min(self.base_delay * (2 ** attempt) + random.uniform(0, 0.3), max_delay)
                logger.warning(
                    f"[LLM-stream] [{get_trace_id()}] retry {attempt+1}/{self.max_retries}: {e}, "
                    f"wait {delay:.1f}s"
                )
                await asyncio.sleep(delay)

        if self.circuit_breaker:
            await self.circuit_breaker.record_failure()
        raise Exception(f"LLM 流式调用失败（已重试 {self.max_retries} 次），请稍后重试")
