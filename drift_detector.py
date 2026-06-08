"""
对话漂移检测模块
- 从 session_manager.py 拆分（v4.3）
- 4 类漂移检测：话题/意图/矛盾/重复
- jieba 中文分词 + 多分类意图 + 扩充矛盾表
- 漂移频率升级机制
- 漂移自动修复策略
"""
from typing import Dict, List, Any, Optional

from logger import get_logger
from config import (
    DRIFT_TOPIC_JACCARD_THRESHOLD as _CFG_TOPIC_THRESHOLD,
    DRIFT_REPETITION_THRESHOLD as _CFG_REP_THRESHOLD,
    DRIFT_ESCALATION_THRESHOLD as _CFG_ESCALATION_THRESHOLD,
)
from token_counter import _tokenize_chinese

logger = get_logger("drift_detector")


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


def _classify_intent(text: str) -> Optional[str]:
    """多分类意图识别（v3.1: 7 类意图）"""
    scores = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in text)
        if count > 0:
            scores[intent] = count
    if not scores:
        return None
    return max(scores, key=scores.get)


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


def _text_similarity(a: str, b: str) -> float:
    """文本相似度（v3.1: jieba 分词提升精度）"""
    sa = _tokenize_chinese(a)
    sb = _tokenize_chinese(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class DriftDetector:
    """
    漂移检测器
    - 4 类对话漂移识别（jieba 分词 + 多分类意图 + 扩充矛盾表）
    - 漂移频率升级机制
    """

    def detect(self, session: Dict[str, Any], session_id: str, current_query: str) -> Dict[str, Any]:
        """
        检测 4 类对话漂移（v3.1 增强）
        - jieba 中文分词提升话题/相似度检测精度
        - 多分类意图漂移（7 类）
        - 扩充矛盾反义词表（40+ 组）
        - 漂移频率升级机制
        """
        topic_history = list(session.get("topic_history", []))
        drifts = []

        if len(topic_history) < 2:
            # v3.1: 即使历史不足也检查漂移升级
            escalation = _check_escalation(session, session_id)
            return {"has_drift": False, "drifts": [], "escalation": escalation}

        recent = topic_history[-6:]

        # 1. 重复提问检测（v3.1: jieba 分词）
        user_queries = [h["content"] for h in recent if h["is_user"]]
        threshold_rep = _CFG_REP_THRESHOLD
        for i, q in enumerate(user_queries[:-1]):
            similarity = _text_similarity(q, current_query)
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
        prev_intent = _classify_intent(prev_text)
        curr_intent = _classify_intent(current_query)
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
        escalation = _check_escalation(session, session_id)

        return {
            "has_drift": len(drifts) > 0,
            "drifts": drifts,
            "escalation": escalation,
        }
