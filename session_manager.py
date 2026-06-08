"""
增强会话管理器（v4.1）
- 滑动窗口裁剪 + 历史摘要策略（v3.1: token 级裁剪）
- 话题漂移检测（4类：话题/意图/矛盾/重复）
- v3.1: jieba 中文分词提升话题/相似度检测精度
- v3.1: 多分类意图漂移检测（7 类，接入 Router 意图）
- v3.1: 扩充矛盾检测反义词表（40+ 组）
- v3.1: 漂移频率升级机制
- v3.1: tiktoken token 计数
- 漂移自动修复上下文生成
- 支持多种存储后端
- 结构化日志
- 兼容新版 langchain（移除旧 memory 模块依赖）
- v4.1: 关键方法改为全异步，消除 asyncio.to_thread 线程池开销
- v4.3: 拆分为 token_counter / drift_detector / session_manager 三模块
"""

import asyncio
import functools
import hashlib
import hmac
import inspect
import json
import os
import re
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional

from config import (
    DRIFT_ESCALATION_THRESHOLD as _CFG_ESCALATION_THRESHOLD,
)
from config import (
    DRIFT_REPETITION_THRESHOLD as _CFG_REP_THRESHOLD,
)
from config import (
    DRIFT_TOPIC_JACCARD_THRESHOLD as _CFG_TOPIC_THRESHOLD,
)
from config import (
    MAX_SESSIONS as _CFG_MAX_SESSIONS,
)
from config import (
    REDIS_SESSION_PREFIX as _CFG_REDIS_PREFIX,
)
from config import (
    REDIS_URL as _CFG_REDIS_URL,
)
from config import (
    SESSION_IDLE_TTL as _CFG_SESSION_IDLE_TTL,
)
from config import (
    SESSION_MAX_TOKENS as _CFG_SESSION_MAX_TOKENS,
)
from config import (
    SESSION_SUMMARY_MAX_CHARS as _CFG_SUMMARY_MAX_CHARS,
)
from drift_detector import (
    DRIFT_REPAIR_STRATEGIES,
    INTENT_KEYWORDS,
    NEGATION_PAIRS,
    DriftDetector,
    DriftType,
)
from logger import get_logger

# v4.3: 从拆分模块导入，保持所有原有公开符号可从 session_manager 导入
from token_counter import _count_tokens, _get_jieba, _get_tokenizer, _tokenize_chinese

logger = get_logger("session_manager")

# v3.6: 会话 ID 格式校验（防路径遍历 / 注入）
_SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9\-_]{1,64}$")


# ---------------------------------------------------------------------------
# 同步/异步兼容层
# ---------------------------------------------------------------------------
# _dual_mode 装饰器：让 async 方法同时支持同步调用。
#
# 设计决策（保守方案）：
#   原实现每次同步调用都 asyncio.run() 创建新事件循环。
#   我们评估了"缓存共享事件循环"方案（_loop = asyncio.new_event_loop()），
#   但存在以下风险：
#     1. run_until_complete() 在已有 task 运行时会抛 RuntimeError（嵌套调用）
#     2. 事件循环关闭时机难以把控（进程退出时可能残留未关闭的 loop）
#     3. 混合 asyncio.run() 和缓存 loop 会导致 asyncio.run() 关闭共享 loop
#   因此采用更保守的改进：保留 asyncio.run() 但添加结构化日志，
#   便于监控同步调用频率，为后续迁移到纯异步接口提供数据支撑。
# ---------------------------------------------------------------------------

_loop_creation_count = 0  # 统计同步调用创建新事件循环的次数


def _run_async_compat(coro):
    """向后兼容：在同步上下文中运行 async 协程。
    无事件循环时用 asyncio.run()，已有循环时用 asyncio.ensure_future。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return asyncio.run(coro)
    return asyncio.ensure_future(coro)


def _dual_mode(async_func):
    """装饰器 -- 让 async 方法同时支持同步调用。

    同步调用时用 asyncio.run() 执行；异步调用时正常返回协程（可被 await）。
    v5.0: 添加结构化日志，跟踪同步调用频率以指导后续纯异步迁移。
    """

    @functools.wraps(async_func)
    def wrapper(*args, **kwargs):
        global _loop_creation_count
        coro = async_func(*args, **kwargs)
        if inspect.iscoroutine(coro):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # 同步上下文：需要创建新事件循环
                _loop_creation_count += 1
                if _loop_creation_count <= 5 or _loop_creation_count % 100 == 0:
                    logger.warning(
                        f"[DualMode] 同步调用 async 方法 '{async_func.__name__}'，"
                        f"创建新事件循环（累计 {_loop_creation_count} 次）。"
                        f"建议改用异步 API 以避免循环创建开销。"
                    )
                return asyncio.run(coro)
            else:
                # 异步上下文：返回协程，让调用方 await
                return coro
        return coro

    # 保留原始 async 函数的引用，供需要直接调用的场景使用
    wrapper._original = async_func
    return wrapper


class EnhancedSessionManager:
    """
    增强会话管理器
    ──────────────────────────────────────────────────────────────────────
    组织结构（590 行，已在 v4.3 拆出 token_counter + drift_detector）：

    [S1] 初始化与存储后端配置 ............ __init__, _validate_storage_config
    [S2] 存储后端适配 .................... _get_redis, _create_memory_backend,
                                          _save_to_file
    [S3] 会话生命周期 .................... _evict_idle_sessions, create_session,
                                          get_session, _delete_session_unlocked
    [S4] 会话所有权令牌 .................. _get_token_secret, generate_session_token,
                                          validate_session_token
    [S5] 用户级隔离 ...................... set_user_id
    [S6] 消息存储与滑动窗口 .............. add_message, get_conversation_context,
                                          _messages_to_context, _generate_summary_async
    [S7] 漂移检测（委托 drift_detector）.. _classify_intent, detect_drift,
                                          _check_escalation, _text_similarity
    [S8] 管理/查询接口 .................. get_session_info, list_sessions,
                                          list_sessions_brief, delete_session
    [S9] 同步兼容包装 .................... *_sync 方法

    注：token 计数已拆至 token_counter.py，漂移检测已拆至 drift_detector.py。
    本文件 590 行主要为存储后端（S2）+ 会话生命周期（S3）+ 消息操作（S6），
    拆分收益不大，保持单文件。
    ──────────────────────────────────────────────────────────────────────
    """

    # ------------------------------------------------------------------
    # [S1] 初始化与存储后端配置
    # ------------------------------------------------------------------

    def __init__(
        self,
        storage_backend: str = "memory",
        window_size: int = 10,
        llm=None,
        max_tokens: int = None,
        **storage_config,
    ):
        self.storage_backend = storage_backend
        self.storage_config = storage_config
        self.window_size = window_size
        self.max_tokens = max_tokens or _CFG_SESSION_MAX_TOKENS
        self.llm = llm
        self.sessions: dict[str, dict[str, Any]] = {}
        self._create_count = 0  # v3.8 fix: explicit init (was hasattr dynamic)
        self._session_lock = (
            asyncio.Lock()
        )  # v3.8 fix: protect sessions dict from concurrent mutation
        self._drift_detector = DriftDetector()  # v4.3: 委托漂移检测
        self._validate_storage_config()
        logger.info(
            f"初始化完成 backend={storage_backend} window={window_size} max_tokens={self.max_tokens}"
        )

    def _validate_storage_config(self):
        defaults = {
            "redis": {"url": _CFG_REDIS_URL, "ttl": 86400},
            "file": {"storage_dir": "./chat_sessions"},
        }
        cfg = defaults.get(self.storage_backend, {})
        for k, v in cfg.items():
            self.storage_config.setdefault(k, v)
        if self.storage_backend == "file":
            os.makedirs(self.storage_config["storage_dir"], exist_ok=True)

    # ------------------------------------------------------------------
    # [S2] 存储后端适配（内存 / 文件 / Redis）
    # ------------------------------------------------------------------

    def _get_redis(self):
        """获取 Redis 客户端（懒初始化，失败时回退到内存模式）"""
        if not hasattr(self, "_redis_client") or self._redis_client is None:
            try:
                import redis

                url = self.storage_config.get("url", _CFG_REDIS_URL)
                self._redis_client = redis.Redis.from_url(url, decode_responses=True)
                self._redis_client.ping()
                # 仅记录主机信息，不暴露完整 URL（可能含密码）
                safe_url = url.split("@")[-1] if "@" in url else url
                logger.info(f"Redis 连接成功: {safe_url}")
            except Exception as e:
                logger.warning(f"Redis 连接失败，回退到内存模式: {e}")
                self._redis_client = None
                self.storage_backend = "memory"
        return self._redis_client

    def _create_memory_backend(self, session_id: str) -> list:
        """创建简单消息存储（兼容新版 langchain，不依赖已废弃的 ConversationBufferMemory）"""
        # 所有后端统一使用内存列表存储
        # 文件后端可从 JSON 加载历史
        if self.storage_backend == "file":
            fp = os.path.join(
                self.storage_config.get("storage_dir", "./chat_sessions"), f"{session_id}.json"
            )
            if os.path.exists(fp):
                try:
                    with open(fp, encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    logger.warning(f"加载会话文件失败 session={session_id}: {e}")
        return []

    def _save_to_file(self, session_id: str, messages: list):
        """文件后端持久化"""
        if self.storage_backend == "file":
            fp = os.path.join(
                self.storage_config.get("storage_dir", "./chat_sessions"), f"{session_id}.json"
            )
            try:
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(messages, f, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"文件保存失败: {e}")

    # ------------------------------------------------------------------
    # [S3] 会话生命周期（创建 / 获取 / 淘汰 / 删除）
    # ------------------------------------------------------------------

    # ---- 会话生命周期 ----

    def _evict_idle_sessions(self):
        """v3.4: 淘汰超过上限的空闲会话，防止内存无限增长"""
        import config  # 动态读取，支持运行时修改

        now = time.time()
        max_sessions = config.MAX_SESSIONS
        idle_ttl = config.SESSION_IDLE_TTL

        # 先淘汰超过空闲 TTL 的会话
        expired = [
            sid for sid, s in self.sessions.items() if now - s.get("last_activity", 0) > idle_ttl
        ]
        for sid in expired:
            self._delete_session_unlocked(sid)

        # 如果仍然超过上限，淘汰最旧的会话
        if len(self.sessions) >= max_sessions:
            sorted_sessions = sorted(
                self.sessions.items(), key=lambda x: x[1].get("last_activity", 0)
            )
            to_remove = len(self.sessions) - max_sessions + 1
            for sid, _ in sorted_sessions[:to_remove]:
                self._delete_session_unlocked(sid)
            logger.warning(f"[Session] 会话数超限，淘汰 {to_remove} 个旧会话")

    @_dual_mode
    async def create_session(self, session_id: str = None) -> str:
        """创建会话（异步版，文件/Redis I/O 通过 asyncio.to_thread 非阻塞执行）"""
        if session_id is None:
            session_id = str(uuid.uuid4())
        elif not _SESSION_ID_PATTERN.match(session_id):
            session_id = str(uuid.uuid4())  # Invalid ID, generate new one
            logger.warning("Invalid session_id format, generated new UUID")
        messages = await asyncio.to_thread(self._create_memory_backend, session_id)

        # v3.8 fix: protect sessions dict with asyncio.Lock
        async with self._session_lock:
            # v3.6: 节流淘汰（每 50 次创建检查一次，避免 O(N) 扫描）
            self._create_count += 1
            if self._create_count % 50 == 0:
                self._evict_idle_sessions()

        # Redis 后端：尝试从 Redis 加载已有会话
        if self.storage_backend == "redis":
            r = self._get_redis()
            if r:
                try:

                    def _redis_load():
                        stored = r.get(f"{_CFG_REDIS_PREFIX}{session_id}:messages")
                        if stored:
                            msgs = json.loads(stored)
                            meta_raw = r.get(f"{_CFG_REDIS_PREFIX}{session_id}:meta")
                            meta = json.loads(meta_raw) if meta_raw else {}
                            return msgs, meta
                        return None, None

                    stored, meta = await asyncio.to_thread(_redis_load)
                    if stored is not None:
                        messages = stored
                        self.sessions[session_id] = {
                            "messages": messages,
                            "memory": messages,
                            "storage_backend": self.storage_backend,
                            "created_at": meta.get("created_at", time.time()),
                            "last_activity": meta.get("last_activity", time.time()),
                            "message_count": len(messages),
                            "summary": meta.get("summary", ""),
                            "user_id": meta.get("user_id", ""),
                            "drift_log": meta.get("drift_log", []),
                            "topic_history": deque(meta.get("topic_history", []), maxlen=50),
                        }
                        logger.debug(f"从 Redis 加载会话: {session_id}")
                        return session_id
                except Exception as e:
                    logger.warning(f"Redis 读取失败: {e}")

        self.sessions[session_id] = {
            "messages": messages,
            "memory": messages,  # 兼容旧引用
            "storage_backend": self.storage_backend,
            "created_at": time.time(),
            "last_activity": time.time(),
            "message_count": len(messages),
            "summary": "",
            "user_id": "",
            "drift_log": [],
            "topic_history": deque(maxlen=50),
        }
        logger.debug(f"创建会话: {session_id}")
        return session_id

    # ------------------------------------------------------------------
    # [S4] 会话所有权令牌（v3.7: HMAC 防会话劫持）
    # ------------------------------------------------------------------

    @staticmethod
    def _get_token_secret() -> str:
        """获取令牌签名密钥（运行时从 config 读取，支持动态配置）"""
        import config

        secret = config.SESSION_TOKEN_SECRET
        # v4.0: 占位符值视为未配置（安全启发式），生产环境应配置真实密钥
        if secret in ("", "change-me-session-secret-in-production"):
            if not getattr(config, "DEV_MODE", False):
                import logging

                logging.getLogger("session_manager").warning(
                    "⚠️ SESSION_TOKEN_SECRET 未配置！会话所有权校验已禁用，生产环境必须配置此密钥。"
                )
            return ""
        return secret

    def generate_session_token(self, session_id: str, client_fingerprint: str = "") -> str:
        """为新会话生成 HMAC 所有权令牌（v4.0: 绑定客户端指纹）"""
        secret = self._get_token_secret()
        if not secret:
            return ""  # 未配置密钥时返回空（向后兼容，不强制校验）
        # v4.0: 将客户端指纹（IP+UA）混入 token 生成，防止跨客户端复用
        data = f"{session_id}:{client_fingerprint}" if client_fingerprint else session_id
        return hmac.new(
            secret.encode("utf-8"),
            data.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]

    def validate_session_token(
        self, session_id: str, token: str, client_fingerprint: str = ""
    ) -> bool:
        """校验会话令牌是否匹配（防止非创建者访问会话）
        v4.0 安全修复: SESSION_TOKEN_SECRET 启用时必须携带有效 token
        v4.0: 绑定客户端指纹（IP+UA），防止跨客户端复用
        """
        secret = self._get_token_secret()
        if not secret:
            if token:
                logger.warning("SESSION_TOKEN_SECRET 未配置，但收到了会话令牌，拒绝验证")
                return False
            # v5.0: 非 DEV 模式下空 secret 应拒绝（防止 IDOR）
            import config

            if not getattr(config, "DEV_MODE", False):
                logger.warning("SESSION_TOKEN_SECRET 未配置，非 DEV 模式拒绝放行")
                return False
            return True  # 仅 DEV 模式放行
        if not token:
            return False
        expected = self.generate_session_token(session_id, client_fingerprint)
        if hmac.compare_digest(token, expected):
            return True
        # v4.0: 回退到无指纹验证（兼容旧 token）
        expected_no_fp = self.generate_session_token(session_id, "")
        return hmac.compare_digest(token, expected_no_fp)

    @_dual_mode
    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """获取会话（异步版，内部 create_session 可能涉及 I/O）"""
        if not _SESSION_ID_PATTERN.match(session_id):
            return None
        if session_id not in self.sessions:
            await self.create_session(session_id)
        return self.sessions[session_id]

    # ------------------------------------------------------------------
    # [S5] 用户级隔离
    # ------------------------------------------------------------------

    # ---- H-3: 用户级隔离 ----

    def set_user_id(self, session_id: str, user_id: str):
        """设置会话的 user_id（用于用户级隔离）
        注意: 简单 dict 赋值受 CPython GIL 保护，无需额外加锁。
        如需严格一致性，可在调用方使用 _session_lock。
        """
        if session_id in self.sessions and user_id:
            self.sessions[session_id]["user_id"] = user_id

    # ------------------------------------------------------------------
    # [S6] 消息存储与滑动窗口裁剪
    # ------------------------------------------------------------------

    # ---- 消息操作 ----

    @_dual_mode
    async def add_message(self, session_id: str, message: str, is_user: bool = True):
        """
        添加消息到会话（v4.1: 异步版，文件/Redis I/O 通过 asyncio.to_thread 非阻塞执行）
        v3.8: 自动用第一条用户消息生成摘要
        """
        session = await self.get_session(session_id)
        msg = {"role": "user" if is_user else "assistant", "content": message}
        session["messages"].append(msg)
        session["message_count"] = len(session["messages"])
        session["last_activity"] = time.time()
        session["topic_history"].append(
            {"is_user": is_user, "content": message[:100], "ts": time.time()}
        )

        # v3.8: 如果是第一条用户消息且没有摘要，用它生成摘要（截取前50字符）
        if is_user and not session.get("summary") and session["message_count"] <= 1:
            session["summary"] = message[:50].strip()
            if len(message) > 50:
                session["summary"] += "..."

        await asyncio.to_thread(self._save_to_file, session_id, session["messages"])

        # Redis 后端持久化
        if self.storage_backend == "redis":
            r = self._get_redis()
            if r:
                try:
                    ttl = self.storage_config.get("ttl", 86400)

                    def _redis_save():
                        r.setex(
                            f"{_CFG_REDIS_PREFIX}{session_id}:messages",
                            ttl,
                            json.dumps(session["messages"], ensure_ascii=False),
                        )
                        meta = {
                            "created_at": session["created_at"],
                            "last_activity": session["last_activity"],
                            "summary": session.get("summary", ""),
                            "user_id": session.get("user_id", ""),
                            "drift_log": session.get("drift_log", []),
                            "topic_history": list(session.get("topic_history", [])),
                        }
                        r.setex(
                            f"{_CFG_REDIS_PREFIX}{session_id}:meta",
                            ttl,
                            json.dumps(meta, ensure_ascii=False),
                        )

                    await asyncio.to_thread(_redis_save)
                except Exception as e:
                    logger.warning(f"Redis 保存失败: {e}")

    async def get_conversation_context(
        self, session_id: str, max_messages: int = None
    ) -> list[dict[str, Any]]:
        """获取带滑动窗口 + 摘要的对话上下文（v3.4: 异步摘要生成，不阻塞事件循环）"""
        session = await self.get_session(session_id)
        messages = session["messages"]

        if max_messages is None:
            max_messages = self.window_size * 2

        if len(messages) <= max_messages:
            # 即使消息数未超限，也要检查 token
            total_tokens = sum(_count_tokens(m.get("content", "")) for m in messages)
            if total_tokens <= self.max_tokens:
                return self._messages_to_context(messages)

        # v3.1: 优先按 token 数裁剪，同时保留消息数上限
        old_messages = []
        recent_messages = list(messages)

        # 第一步：按消息数裁剪
        if len(recent_messages) > max_messages:
            old_messages = recent_messages[:-max_messages]
            recent_messages = recent_messages[-max_messages:]

        # 第二步：在 recent 中按 token 继续裁剪（保留最近的对话）
        total_tokens = sum(_count_tokens(m.get("content", "")) for m in recent_messages)
        while total_tokens > self.max_tokens and len(recent_messages) > 2:
            removed = recent_messages.pop(0)
            old_messages.append(removed)
            total_tokens -= _count_tokens(removed.get("content", ""))

        if self.llm and old_messages:
            summary = await self._generate_summary_async(old_messages, session.get("summary", ""))
            session["summary"] = summary

        context = []
        if session.get("summary"):
            context.append(
                {"role": "system", "content": f"[历史摘要] {session['summary']}", "is_user": False}
            )
        context.extend(self._messages_to_context(recent_messages))
        return context

    def _messages_to_context(self, messages: list[dict[str, str]]) -> list[dict[str, Any]]:
        """将消息列表转换为上下文格式"""
        result = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            is_user = role == "user"
            result.append({"role": role, "content": content, "is_user": is_user})
        return result

    async def _generate_summary_async(
        self, messages: list[dict[str, str]], existing_summary: str = ""
    ) -> str:
        """v3.4: 异步摘要生成（不阻塞事件循环）"""
        if not self.llm:
            return existing_summary
        try:
            max_chars = _CFG_SUMMARY_MAX_CHARS
            text = "\n".join(
                [
                    f"{'用户' if m.get('role') == 'user' else 'AI'}: {m.get('content', '')[:200]}"
                    for m in messages[-20:]
                ]
            )
            prompt = (
                f"请用 2-3 句话总结以下对话要点（保留关键信息如产品名、订单号、问题类型）：\n{text}"
            )
            if existing_summary:
                prompt = f"已有摘要：{existing_summary}\n\n请结合新对话更新摘要：\n{text}"
            from langchain_core.messages import HumanMessage as HM

            # v3.4: 优先使用异步接口，回退到 asyncio.to_thread
            if hasattr(self.llm, "async_invoke"):
                resp = await self.llm.async_invoke([HM(content=prompt)])
            else:
                resp = await asyncio.to_thread(self.llm.invoke, [HM(content=prompt)])
            return resp.content.strip()[:max_chars]
        except Exception as e:
            logger.warning(f"摘要生成失败: {e}")
            return existing_summary

    # ------------------------------------------------------------------
    # [S7] 漂移检测（委托 drift_detector 模块）
    # ------------------------------------------------------------------

    # ---- 漂移检测 ----

    def _classify_intent(self, text: str) -> str | None:
        """多分类意图识别（v3.1: 7 类意图）"""
        from drift_detector import _classify_intent as _classify

        return _classify(text)

    @_dual_mode
    async def detect_drift(self, session_id: str, current_query: str) -> dict[str, Any]:
        """
        检测 4 类对话漂移（v3.1 增强）
        - jieba 中文分词提升话题/相似度检测精度
        - 多分类意图漂移（7 类）
        - 扩充矛盾反义词表（40+ 组）
        - 漂移频率升级机制
        """
        session = await self.get_session(session_id)
        return self._drift_detector.detect(session, session_id, current_query)

    @staticmethod
    def _check_escalation(session: dict[str, Any], session_id: str = "") -> dict[str, Any] | None:
        """v3.1: 检查漂移频率是否触发升级"""
        from drift_detector import _check_escalation

        return _check_escalation(session, session_id)

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """文本相似度（v3.1: jieba 分词提升精度）"""
        from drift_detector import _text_similarity

        return _text_similarity(a, b)

    # ------------------------------------------------------------------
    # [S8] 管理 / 查询接口
    # ------------------------------------------------------------------

    # ---- 管理接口 ----

    def get_session_info(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if not session:
            return {}
        drift_count = len(session.get("drift_log", []))
        escalation_threshold = _CFG_ESCALATION_THRESHOLD
        return {
            "session_id": session_id,
            "message_count": session["message_count"],
            "last_activity": session["last_activity"],
            "summary": session.get("summary", ""),
            "drift_count": drift_count,
            "drift_escalation": drift_count >= escalation_threshold,
        }

    @_dual_mode
    async def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有会话（异步版，保持锁保护一致性）"""
        async with self._session_lock:
            return [self.get_session_info(sid) for sid in self.sessions]

    @_dual_mode
    async def list_sessions_brief(self, offset: int = 0, limit: int = 20) -> dict:
        """一次性返回会话摘要列表（避免 N+1 查询）"""
        async with self._session_lock:
            all_sessions = []
            for sid, s in self.sessions.items():
                msgs = s.get("messages", [])
                last_msg = msgs[-1].get("content", "") if msgs else ""
                all_sessions.append(
                    {
                        "session_id": sid,
                        "title": msgs[0].get("content", "")[:50] if msgs else "",
                        "last_message": last_msg[:100],
                        "message_count": s.get("message_count", len(msgs)),
                        "updated_at": s.get("last_activity", 0),
                        "user_id": s.get("user_id", ""),
                    }
                )
            # 按更新时间倒序
            all_sessions.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
            total = len(all_sessions)
            return {
                "sessions": all_sessions[offset : offset + limit],
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    @_dual_mode
    async def delete_session(self, session_id: str):
        """删除会话（异步版，Redis 清理通过 asyncio.to_thread 非阻塞执行）"""
        async with self._session_lock:
            self._delete_session_unlocked(session_id)

    def _delete_session_unlocked(self, session_id: str):
        """v3.8: internal delete without lock (called by _evict_idle_sessions which already holds lock)"""
        if session_id in self.sessions:
            self.sessions[session_id]["messages"].clear()
            del self.sessions[session_id]

        # Redis 后端清理
        if self.storage_backend == "redis":
            r = self._get_redis()
            if r:
                try:
                    r.delete(f"{_CFG_REDIS_PREFIX}{session_id}:messages")
                    r.delete(f"{_CFG_REDIS_PREFIX}{session_id}:meta")
                except Exception as e:
                    logger.warning(f"Redis 会话清理失败 session={session_id}: {e}")

    # ------------------------------------------------------------------
    # [S9] 向后兼容：同步包装方法
    # ------------------------------------------------------------------

    # ---- 向后兼容：同步包装（v4.1） ----

    def create_session_sync(self, session_id: str = None) -> str:
        """create_session 的同步包装"""
        return _run_async_compat(self.create_session(session_id))

    def get_session_sync(self, session_id: str) -> dict[str, Any] | None:
        """get_session 的同步包装"""
        return _run_async_compat(self.get_session(session_id))

    def add_message_sync(self, session_id: str, message: str, is_user: bool = True):
        """add_message 的同步包装"""
        return _run_async_compat(self.add_message(session_id, message, is_user))

    async def get_conversation_context_sync(
        self, session_id: str, max_messages: int = None
    ) -> list[dict[str, Any]]:
        """get_conversation_context 的同步包装（注意：原方法已是 async，此方法仅为命名兼容）"""
        return await self.get_conversation_context(session_id, max_messages)

    def delete_session_sync(self, session_id: str):
        """delete_session 的同步包装"""
        return _run_async_compat(self.delete_session(session_id))

    def list_sessions_sync(self) -> list[dict[str, Any]]:
        """list_sessions 的同步包装"""
        return _run_async_compat(self.list_sessions())

    def generate_session_token_sync(self, session_id: str, client_fingerprint: str = "") -> str:
        """generate_session_token 的同步包装"""
        return _run_async_compat(self.generate_session_token(session_id, client_fingerprint))

    def validate_session_token_sync(
        self, session_id: str, token: str, client_fingerprint: str = ""
    ) -> bool:
        """validate_session_token 的同步包装"""
        return _run_async_compat(self.validate_session_token(session_id, token, client_fingerprint))


# 默认实例
default_session_manager = EnhancedSessionManager()
