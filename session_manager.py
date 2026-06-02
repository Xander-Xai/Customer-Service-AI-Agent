"""
增强会话管理器（v3.1）
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
"""
import os
import re
import time
import uuid
import json
import hmac
import hashlib
from typing import Dict, List, Any, Optional
from collections import deque

from logger import get_logger
from config import (
    SESSION_MAX_TOKENS as _CFG_SESSION_MAX_TOKENS,
    SESSION_SUMMARY_MAX_CHARS as _CFG_SUMMARY_MAX_CHARS,
    DRIFT_TOPIC_JACCARD_THRESHOLD as _CFG_TOPIC_THRESHOLD,
    DRIFT_REPETITION_THRESHOLD as _CFG_REP_THRESHOLD,
    DRIFT_ESCALATION_THRESHOLD as _CFG_ESCALATION_THRESHOLD,
    REDIS_URL as _CFG_REDIS_URL,
    MAX_SESSIONS as _CFG_MAX_SESSIONS,
    SESSION_IDLE_TTL as _CFG_SESSION_IDLE_TTL,
)

logger = get_logger("session_manager")

# v3.6: 会话 ID 格式校验（防路径遍历 / 注入）
_SESSION_ID_PATTERN = re.compile(r'^[a-zA-Z0-9\-_]{1,64}$')

# 中文分词（懒加载）
_jieba = None
_jieba_loaded = False

def _get_jieba():
    """懒加载 jieba 分词器（静默模式）"""
    global _jieba, _jieba_loaded
    if not _jieba_loaded:
        _jieba_loaded = True
        try:
            import jieba
            jieba.setLogLevel(jieba.logging.WARNING)
            _jieba = jieba
            logger.info("jieba 中文分词已加载")
        except ImportError:
            logger.warning("jieba 未安装，回退到正则分词")
            _jieba = None
    return _jieba

# tiktoken 编码器（懒加载）
_tokenizer = None
_tokenizer_loaded = False

def _get_tokenizer():
    """懒加载 tiktoken 编码器"""
    global _tokenizer, _tokenizer_loaded
    if not _tokenizer_loaded:
        _tokenizer_loaded = True
        try:
            import tiktoken
            _tokenizer = tiktoken.get_encoding("cl100k_base")
            logger.info("tiktoken 编码器已加载")
        except ImportError:
            logger.warning("tiktoken 未安装，回退到字符估算")
            _tokenizer = None
    return _tokenizer


def _tokenize_chinese(text: str) -> set:
    """中文分词 token 化（v3.4: 过滤单字停用词，与正则回退保持一致）"""
    jb = _get_jieba()
    if jb:
        return set(w for w in jb.cut(text) if len(w.strip()) >= 2)
    # 回退：正则提取中文词（2字+）和英文词
    return set(re.findall(r"[\w一-鿿]{2,}", text.lower()))


def _count_tokens(text: str) -> int:
    """计算文本 token 数（tiktoken 优先，回退字符估算）"""
    if not text:
        return 0
    tok = _get_tokenizer()
    if tok:
        return len(tok.encode(text))
    # 回退：中文约 1.5 字/token，英文约 4 字符/token
    cn_chars = len(re.findall(r"[一-鿿]", text))
    other_chars = len(text) - cn_chars
    return int(cn_chars / 1.5 + other_chars / 4)


class DriftType:
    TOPIC = "topic_drift"
    INTENT = "intent_drift"
    CONTRADICTION = "contradiction"
    REPETITION = "repetition"


# 漂移修复策略映射（v3.0 新增）
DRIFT_REPAIR_STRATEGIES = {
    DriftType.TOPIC: "话题漂移：先简短确认用户新需求，再回答新问题，询问是否还需要之前的解答",
    DriftType.INTENT: "意图漂移：调整响应策略，说明服务切换，确保用户了解处理方式变更",
    DriftType.CONTRADICTION: "矛盾检测：温和指出矛盾点，请求用户确认真实需求",
    DriftType.REPETITION: "重复提问：参考之前回答提供精炼回复，询问是否需要更详细解释",
}

# 扩充反义词/矛盾对（v3.1: 40+ 组）
NEGATION_PAIRS = [
    # 情感/评价
    ("好", "差"), ("满意", "不满"), ("喜欢", "讨厌"), ("推荐", "不推荐"),
    ("不错", "很差"), ("优秀", "糟糕"), ("完美", "缺陷"),
    # 效果
    ("有效", "无效"), ("有用", "没用"), ("改善", "恶化"), ("好转", "变差"),
    ("白了", "没白"), ("保湿", "干燥"), ("修复", "损伤"),
    # 态度/意愿
    ("愿意", "不愿意"), ("想买", "不想买"), ("要", "不要"),
    ("接受", "拒绝"), ("同意", "反对"), ("支持", "反对"),
    # 数量/程度
    ("很多", "很少"), ("太贵", "便宜"), ("太慢", "快"),
    ("太多", "太少"), ("严重", "轻微"),
    # 时间/顺序
    ("之前", "现在"), ("以前", "最近"), ("一直", "从不"),
    ("经常", "从不"), ("总是", "偶尔"),
    # 安全/品质
    ("安全", "危险"), ("正品", "假货"), ("天然", "化学"),
    ("温和", "刺激"), ("不过敏", "过敏"),
    # 服务
    ("及时", "拖延"), ("专业", "不专业"), ("负责", "不负责"),
    ("解决了", "没解决"), ("可以退", "不能退"),
]

# 多分类意图关键词映射（v3.1: 7 类意图，接入 Router 体系）
INTENT_KEYWORDS = {
    "product_info": ["产品", "商品", "精华", "面膜", "成分", "功效", "价格", "多少钱", "哪款"],
    "technical_support": ["过敏", "刺激", "红肿", "怎么用", "用法", "保质期", "保存", "搭配"],
    "billing": ["退款", "退货", "发票", "付款", "支付", "账单", "费用"],
    "complaint": ["投诉", "不满", "差评", "态度差", "服务差", "不负责", "举报", "经理"],
    "order_query": ["订单", "物流", "快递", "发货", "到货", "签收", "运单"],
    "cosmetic_advice": ["肤质", "油性", "干性", "敏感", "美白", "保湿", "抗皱", "护肤"],
    "general_inquiry": ["你好", "请问", "想问", "咨询", "了解", "介绍"],
}


class EnhancedSessionManager:
    """
    增强会话管理器（v3.1）
    - 滑动窗口：token 级裁剪 + 消息数双重控制
    - 漂移检测：4 类对话漂移识别（jieba 分词 + 多分类意图 + 扩充矛盾表）
    - 漂移升级：频率过高时自动生成升级提示
    - 漂移修复策略生成
    """

    def __init__(self, storage_backend: str = "memory", window_size: int = 10,
                 llm=None, max_tokens: int = None, **storage_config):
        self.storage_backend = storage_backend
        self.storage_config = storage_config
        self.window_size = window_size
        self.max_tokens = max_tokens or _CFG_SESSION_MAX_TOKENS
        self.llm = llm
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self._create_count = 0  # v3.8 fix: explicit init (was hasattr dynamic)
        import threading
        self._session_lock = threading.Lock()  # v3.8 fix: protect sessions dict from concurrent mutation
        self._validate_storage_config()
        logger.info(f"初始化完成 backend={storage_backend} window={window_size} max_tokens={self.max_tokens}")

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

    def _get_redis(self):
        """获取 Redis 客户端（懒初始化，失败时回退到内存模式）"""
        if not hasattr(self, '_redis_client') or self._redis_client is None:
            try:
                import redis
                url = self.storage_config.get("url", _CFG_REDIS_URL)
                self._redis_client = redis.Redis.from_url(url, decode_responses=True)
                self._redis_client.ping()
                logger.info(f"Redis 连接成功: {url}")
            except Exception as e:
                logger.warning(f"Redis 连接失败，回退到内存模式: {e}")
                self._redis_client = None
                self.storage_backend = "memory"
        return self._redis_client

    def _create_memory_backend(self, session_id: str) -> List:
        """创建简单消息存储（兼容新版 langchain，不依赖已废弃的 ConversationBufferMemory）"""
        # 所有后端统一使用内存列表存储
        # 文件后端可从 JSON 加载历史
        if self.storage_backend == "file":
            fp = os.path.join(self.storage_config.get("storage_dir", "./chat_sessions"), f"{session_id}.json")
            if os.path.exists(fp):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    logger.warning(f"加载会话文件失败 session={session_id}: {e}")
        return []

    def _save_to_file(self, session_id: str, messages: List):
        """文件后端持久化"""
        if self.storage_backend == "file":
            fp = os.path.join(self.storage_config.get("storage_dir", "./chat_sessions"), f"{session_id}.json")
            try:
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(messages, f, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"文件保存失败: {e}")

    # ---- 会话生命周期 ----

    def _evict_idle_sessions(self):
        """v3.4: 淘汰超过上限的空闲会话，防止内存无限增长"""
        import config  # 动态读取，支持运行时修改
        now = time.time()
        max_sessions = config.MAX_SESSIONS
        idle_ttl = config.SESSION_IDLE_TTL

        # 先淘汰超过空闲 TTL 的会话
        expired = [sid for sid, s in self.sessions.items()
                   if now - s.get("last_activity", 0) > idle_ttl]
        for sid in expired:
            self._delete_session_unlocked(sid)

        # 如果仍然超过上限，淘汰最旧的会话
        if len(self.sessions) >= max_sessions:
            sorted_sessions = sorted(
                self.sessions.items(),
                key=lambda x: x[1].get("last_activity", 0)
            )
            to_remove = len(self.sessions) - max_sessions + 1
            for sid, _ in sorted_sessions[:to_remove]:
                self._delete_session_unlocked(sid)
            logger.warning(f"[Session] 会话数超限，淘汰 {to_remove} 个旧会话")

    def create_session(self, session_id: str = None) -> str:
        if session_id is None:
            session_id = str(uuid.uuid4())
        elif not _SESSION_ID_PATTERN.match(session_id):
            session_id = str(uuid.uuid4())  # Invalid ID, generate new one
            logger.warning("Invalid session_id format, generated new UUID")
        messages = self._create_memory_backend(session_id)

        # v3.8 fix: protect sessions dict with threading.Lock
        with self._session_lock:
            # v3.6: 节流淘汰（每 50 次创建检查一次，避免 O(N) 扫描）
            self._create_count += 1
            if self._create_count % 50 == 0:
                self._evict_idle_sessions()

        # Redis 后端：尝试从 Redis 加载已有会话
        if self.storage_backend == "redis":
            r = self._get_redis()
            if r:
                try:
                    stored = r.get(f"session:{session_id}:messages")
                    if stored:
                        messages = json.loads(stored)
                        # 也加载元数据
                        meta_raw = r.get(f"session:{session_id}:meta")
                        meta = json.loads(meta_raw) if meta_raw else {}
                        self.sessions[session_id] = {
                            "messages": messages,
                            "memory": messages,
                            "storage_backend": self.storage_backend,
                            "created_at": meta.get("created_at", time.time()),
                            "last_activity": meta.get("last_activity", time.time()),
                            "message_count": len(messages),
                            "summary": meta.get("summary", ""),
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
            "drift_log": [],
            "topic_history": deque(maxlen=50),
        }
        logger.debug(f"创建会话: {session_id}")
        return session_id

    # ---- 会话所有权令牌（v3.7: 防会话劫持）----

    @staticmethod
    def _get_token_secret() -> str:
        """获取令牌签名密钥（运行时从 config 读取，支持动态配置）"""
        import config
        secret = config.SESSION_TOKEN_SECRET
        # v3.8: 占位符值视为未配置（安全启发式）
        if secret in ("", "change-me-session-secret-in-production"):
            return ""
        return secret

    def generate_session_token(self, session_id: str) -> str:
        """为新会话生成 HMAC 所有权令牌，客户端需携带此令牌才能操作会话"""
        secret = self._get_token_secret()
        if not secret:
            return ""  # 未配置密钥时返回空（向后兼容，不强制校验）
        return hmac.new(
            secret.encode("utf-8"),
            session_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]

    def validate_session_token(self, session_id: str, token: str) -> bool:
        """校验会话令牌是否匹配（防止非创建者访问会话）"""
        secret = self._get_token_secret()
        if not secret:
            # v3.8: 未配置密钥时，允许未携带令牌的请求（向后兼容），但拒绝错误令牌
            if token:
                logger.warning("SESSION_TOKEN_SECRET 未配置，但收到了会话令牌，拒绝验证")
                return False
            return True  # 无令牌 + 无密钥 = 放行（开发/测试环境）
        if not token:
            return False
        expected = self.generate_session_token(session_id)
        return hmac.compare_digest(token, expected)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        if not _SESSION_ID_PATTERN.match(session_id):
            return None
        if session_id not in self.sessions:
            self.create_session(session_id)
        return self.sessions[session_id]

    # ---- 消息操作 ----

    def add_message(self, session_id: str, message: str, is_user: bool = True):
        session = self.get_session(session_id)
        msg = {"role": "user" if is_user else "assistant", "content": message}
        session["messages"].append(msg)
        session["message_count"] = len(session["messages"])
        session["last_activity"] = time.time()
        session["topic_history"].append({"is_user": is_user, "content": message[:100], "ts": time.time()})
        self._save_to_file(session_id, session["messages"])

        # Redis 后端持久化
        if self.storage_backend == "redis":
            r = self._get_redis()
            if r:
                try:
                    ttl = self.storage_config.get("ttl", 86400)
                    r.setex(f"session:{session_id}:messages", ttl, json.dumps(session["messages"], ensure_ascii=False))
                    meta = {
                        "created_at": session["created_at"],
                        "last_activity": session["last_activity"],
                        "summary": session.get("summary", ""),
                        "drift_log": session.get("drift_log", []),
                        "topic_history": list(session.get("topic_history", [])),
                    }
                    r.setex(f"session:{session_id}:meta", ttl, json.dumps(meta, ensure_ascii=False))
                except Exception as e:
                    logger.warning(f"Redis 保存失败: {e}")

    async def get_conversation_context(self, session_id: str, max_messages: int = None) -> List[Dict[str, Any]]:
        """获取带滑动窗口 + 摘要的对话上下文（v3.4: 异步摘要生成，不阻塞事件循环）"""
        session = self.get_session(session_id)
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
            context.append({"role": "system", "content": f"[历史摘要] {session['summary']}", "is_user": False})
        context.extend(self._messages_to_context(recent_messages))
        return context

    def _messages_to_context(self, messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """将消息列表转换为上下文格式"""
        result = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            is_user = role == "user"
            result.append({"role": role, "content": content, "is_user": is_user})
        return result

    async def _generate_summary_async(self, messages: List[Dict[str, str]], existing_summary: str = "") -> str:
        """v3.4: 异步摘要生成（不阻塞事件循环）"""
        if not self.llm:
            return existing_summary
        try:
            max_chars = _CFG_SUMMARY_MAX_CHARS
            text = "\n".join([f"{'用户' if m.get('role') == 'user' else 'AI'}: {m.get('content', '')[:200]}" for m in messages[-20:]])
            prompt = f"请用 2-3 句话总结以下对话要点（保留关键信息如产品名、订单号、问题类型）：\n{text}"
            if existing_summary:
                prompt = f"已有摘要：{existing_summary}\n\n请结合新对话更新摘要：\n{text}"
            from langchain_core.messages import HumanMessage as HM
            # v3.4: 优先使用异步接口，回退到 asyncio.to_thread
            if hasattr(self.llm, 'async_invoke'):
                resp = await self.llm.async_invoke([HM(content=prompt)])
            else:
                import asyncio
                resp = await asyncio.to_thread(self.llm.invoke, [HM(content=prompt)])
            return resp.content.strip()[:max_chars]
        except Exception as e:
            logger.warning(f"摘要生成失败: {e}")
            return existing_summary

    # ---- 漂移检测 ----

    def _classify_intent(self, text: str) -> Optional[str]:
        """多分类意图识别（v3.1: 7 类意图）"""
        scores = {}
        for intent, keywords in INTENT_KEYWORDS.items():
            count = sum(1 for kw in keywords if kw in text)
            if count > 0:
                scores[intent] = count
        if not scores:
            return None
        return max(scores, key=scores.get)

    def detect_drift(self, session_id: str, current_query: str) -> Dict[str, Any]:
        """
        检测 4 类对话漂移（v3.1 增强）
        - jieba 中文分词提升话题/相似度检测精度
        - 多分类意图漂移（7 类）
        - 扩充矛盾反义词表（40+ 组）
        - 漂移频率升级机制
        """
        session = self.get_session(session_id)
        topic_history = list(session.get("topic_history", []))
        drifts = []

        if len(topic_history) < 2:
            # v3.1: 即使历史不足也检查漂移升级
            escalation = self._check_escalation(session, session_id)
            return {"has_drift": False, "drifts": [], "escalation": escalation}

        recent = topic_history[-6:]

        # 1. 重复提问检测（v3.1: jieba 分词）
        user_queries = [h["content"] for h in recent if h["is_user"]]
        threshold_rep = _CFG_REP_THRESHOLD
        for i, q in enumerate(user_queries[:-1]):
            similarity = self._text_similarity(q, current_query)
            if similarity > threshold_rep:
                drifts.append({
                    "type": DriftType.REPETITION,
                    "detail": f"与第{i+1}轮问题相似度 {similarity:.0%}",
                    "action": DRIFT_REPAIR_STRATEGIES[DriftType.REPETITION],
                })
                break

        # 2. 话题漂移检测（v3.1: jieba 分词替代正则）
        if len(user_queries) >= 2:
            prev_tokens = _tokenize_chinese(user_queries[-2])
            curr_tokens = _tokenize_chinese(current_query)
            # 过滤停用词（单字、标点）
            prev_tokens = {w for w in prev_tokens if len(w) >= 2}
            curr_tokens = {w for w in curr_tokens if len(w) >= 2}
            if prev_tokens and curr_tokens:
                overlap = len(prev_tokens & curr_tokens) / max(len(prev_tokens | curr_tokens), 1)
                threshold_topic = _CFG_TOPIC_THRESHOLD
                if overlap < threshold_topic:
                    drifts.append({
                        "type": DriftType.TOPIC,
                        "detail": f"用户切换了话题（话题重叠 {overlap:.0%}）",
                        "action": DRIFT_REPAIR_STRATEGIES[DriftType.TOPIC],
                    })

        # 3. 意图漂移检测（v3.1: 多分类 7 类意图）
        prev_text = "".join(user_queries[:-1]) if user_queries[:-1] else ""
        prev_intent = self._classify_intent(prev_text)
        curr_intent = self._classify_intent(current_query)
        if prev_intent and curr_intent and prev_intent != curr_intent:
            drifts.append({
                "type": DriftType.INTENT,
                "detail": f"意图从 '{prev_intent}' 变为 '{curr_intent}'",
                "action": DRIFT_REPAIR_STRATEGIES[DriftType.INTENT],
            })

        # 4. 矛盾检测（v3.1: 40+ 组反义词）— 复用已计算的 prev_text
        for pos, neg in NEGATION_PAIRS:
            if (pos in prev_text and neg in current_query) or (neg in prev_text and pos in current_query):
                drifts.append({
                    "type": DriftType.CONTRADICTION,
                    "detail": f"检测到矛盾表达：'{pos}' vs '{neg}'",
                    "action": DRIFT_REPAIR_STRATEGIES[DriftType.CONTRADICTION],
                })
                break  # 一次只报一个矛盾

        if drifts:
            session["drift_log"].extend(drifts)
            logger.info(f"检测到 {len(drifts)} 条漂移: {[d['type'] for d in drifts]}")

        # v3.1: 漂移频率升级机制
        escalation = self._check_escalation(session, session_id)

        return {
            "has_drift": len(drifts) > 0,
            "drifts": drifts,
            "escalation": escalation,
        }

    @staticmethod
    def _check_escalation(session: Dict[str, Any], session_id: str = "") -> Optional[Dict[str, Any]]:
        """v3.1: 检查漂移频率是否触发升级"""
        escalation_threshold = _CFG_ESCALATION_THRESHOLD
        drift_count = len(session.get("drift_log", []))
        if drift_count >= escalation_threshold:
            logger.warning(f"漂移升级触发: session={session_id} drift_count={drift_count}")
            return {
                "escalate": True,
                "reason": f"会话已累计 {drift_count} 次漂移，建议转人工或重置会话",
                "drift_count": drift_count,
            }
        return None

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """文本相似度（v3.1: jieba 分词提升精度）"""
        sa = _tokenize_chinese(a)
        sb = _tokenize_chinese(b)
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    # ---- 管理接口 ----

    def get_session_info(self, session_id: str) -> Dict[str, Any]:
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

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._session_lock:
            return [self.get_session_info(sid) for sid in self.sessions]

    def delete_session(self, session_id: str):
        with self._session_lock:
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
                    r.delete(f"session:{session_id}:messages")
                    r.delete(f"session:{session_id}:meta")
                except Exception as e:
                    logger.warning(f"Redis 会话清理失败 session={session_id}: {e}")


# 默认实例
default_session_manager = EnhancedSessionManager()
