"""
双层查询路由器（v3.0 → v3.3 性能优化版）
优化：
- 正则模式预编译（避免每次调用 re.search 编译开销）
- _rule_classify_and_score 合并了规则分类与复杂度评分为单次遍历
- LLM 分类与规则分类并行执行
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from core.config import LLM_ROUTER_TIMEOUT
from core.logger import get_logger

logger = get_logger("router")


@dataclass
class RoutingResult:
    """路由结果"""

    query_type: str = "general_inquiry"
    agent_name: str = "general_agent"
    complexity: int = 0
    fast_path: bool = True
    confidence: float = 0.0
    raw_llm_result: str = ""
    rule_override: bool = False
    scene: str = "通用"


# 完整意图分类列表
INTENT_CLASSES = [
    "product_info", "recommendation",       # 售前
    "order_status", "return_policy",         # 售后
    "technical_support", "usage_guide",      # 技术
    "complaint", "negative_feedback",        # 投诉
    "greeting", "general",                   # 通用
]

# 场景映射
SCENE_MAPPING = {
    "product_info": "售前咨询",
    "recommendation": "售前咨询",
    "order_status": "售后支持",
    "return_policy": "售后支持",
    "technical_support": "技术答疑",
    "usage_guide": "技术答疑",
    "complaint": "投诉处理",
    "negative_feedback": "投诉处理",
    "greeting": "通用",
    "general": "通用",
    # 旧意图兼容映射
    "general_inquiry": "通用",
    "order_query": "售后支持",
    "billing": "售后支持",
    "cosmetic_advice": "售前咨询",
}

# 意图 → Agent 映射
INTENT_AGENT_MAP = {
    "product_info": "product_agent",
    "recommendation": "sales_agent",
    "technical_support": "tech_agent",
    "usage_guide": "tech_agent",
    "billing": "billing_agent",
    "complaint": "complaint_agent",
    "negative_feedback": "complaint_agent",
    "general_inquiry": "general_agent",
    "greeting": "general_agent",
    "general": "general_agent",
    "order_query": "billing_agent",
    "order_status": "billing_agent",
    "return_policy": "billing_agent",
    "cosmetic_advice": "product_agent",
}

# 规则模式匹配表（预编译正则，避免每次调用编译开销）
_RULE_PATTERNS = {
    "product_info": [
        re.compile(r"产品|商品|精华|面膜|洁面|面霜|化妆水|价格|多少钱|成分|功效|推荐")
    ],
    "recommendation": [
        re.compile(r"推荐|适合|建议|哪种|哪款|什么好")
    ],
    "technical_support": [re.compile(r"过敏|刺激|红肿|痒|使用方法|怎么用|用法|保质期|有效期|保存")],
    "usage_guide": [
        re.compile(r"怎么用|用法|步骤|顺序|使用|方法")
    ],
    "billing": [re.compile(r"退款|退货|发票|付款|支付|账单|费用|订单|物流|快递|发货")],
    "complaint": [re.compile(r"投诉|不满|差评|举报|客服|经理|领导|态度|服务差|质量.*问题")],
    "negative_feedback": [
        re.compile(r"差劲|失望|太差|不好|垃圾|后悔")
    ],
    "order_query": [re.compile(r"订单号|物流|快递|到货|发货|签收|运单")],
    "order_status": [
        re.compile(r"订单|物流|快递|发货|到哪|签收")
    ],
    "return_policy": [
        re.compile(r"退货|退款|换货|退换|退钱")
    ],
    "cosmetic_advice": [re.compile(r"肤质|油性|干性|敏感|美白|保湿|抗皱|祛痘|祛斑|护肤")],
    "greeting": [
        re.compile(r"你好|您好|hi|hello|在吗|有人吗")
    ],
}

# 复杂度评分用的预编译正则
_RE_TECH_TERMS = [
    re.compile(r"过敏|刺激|成分|配方|工艺"),
    re.compile(r"退款|发票|对公|分期"),
    re.compile(r"投诉|升级|主管"),
]
_RE_PRICE = re.compile(r"\d+[\.\d]*\s*[元块]|¥|￥|\d{10,}")

# 意图优先级（同分时高优先级意图胜出，数值越小越优先）
# 投诉 > 账单 > 技术 > 订单 > 售后 > 产品 > 肤质 > 推荐 > 通用
_INTENT_PRIORITY = {
    "complaint": 0,
    "negative_feedback": 1,
    "billing": 2,
    "order_status": 3,
    "return_policy": 3,
    "technical_support": 4,
    "usage_guide": 5,
    "order_query": 6,
    "product_info": 7,
    "cosmetic_advice": 8,
    "recommendation": 8,
    "general_inquiry": 9,
    "greeting": 10,
    "general": 10,
}


class QueryRouter:
    """双层查询路由器"""

    def __init__(self, llm=None, complexity_threshold: int = 50):
        self.llm = llm
        self.complexity_threshold = complexity_threshold

    async def route(
        self,
        query: str,
        conversation_context: str = "",
        user_id: str | None = None,
    ) -> RoutingResult:
        """主路由入口（v3.3: LLM 与规则分类并行执行 + v5.4: 高置信规则捷径）"""
        # 先执行规则分类（纯同步，几乎无开销）
        rule_result = self._rule_classify_and_score(query, conversation_context)
        rule_type, rule_scores, complexity = rule_result

        # v5.4: 路由捷径 — 规则高置信时跳过 LLM 路由调用
        # 判定条件：唯一意图命中（confidence >= 0.75 即可，规则分类本身很精准）
        rule_confidence = self._estimate_rule_confidence(rule_type, rule_scores)
        if rule_type and rule_confidence >= 0.75:
            agent_name = INTENT_AGENT_MAP.get(rule_type, "general_agent")
            fast_path = complexity < self.complexity_threshold
            logger.info(
                f"route: ⚡ 路由捷径 type={rule_type} agent={agent_name} "
                f"rule_confidence={rule_confidence:.2f} (跳过 LLM 路由)"
            )
            return RoutingResult(
                query_type=rule_type,
                agent_name=agent_name,
                complexity=complexity,
                fast_path=fast_path,
                confidence=rule_confidence,
                raw_llm_result="rule_shortcut",
                rule_override=False,
                scene=SCENE_MAPPING.get(rule_type, "通用"),
            )

        # 规则置信不足，走 LLM + 规则并行分类
        llm_task = self._llm_classify(query, conversation_context, user_id=user_id)
        llm_result = await llm_task

        rule_override = False
        final_type = llm_result["query_type"]
        if (
            rule_type
            and rule_type != llm_result["query_type"]
            and llm_result.get("confidence", 0) < 0.7
        ):
            final_type = rule_type
            rule_override = True

        fast_path = complexity < self.complexity_threshold
        agent_name = INTENT_AGENT_MAP.get(final_type, "general_agent")

        logger.debug(
            f"route: type={final_type} agent={agent_name} complexity={complexity} "
            f"confidence={llm_result.get('confidence', 0):.2f} rule_override={rule_override}"
        )

        return RoutingResult(
            query_type=final_type,
            agent_name=agent_name,
            complexity=complexity,
            fast_path=fast_path,
            confidence=llm_result.get("confidence", 0.5),
            raw_llm_result=llm_result.get("raw", ""),
            rule_override=rule_override,
            scene=SCENE_MAPPING.get(final_type, "通用"),
        )

    def _estimate_rule_confidence(
        self, rule_type: str | None, rule_scores: dict[str, int]
    ) -> float:
        """估算规则分类的置信度

        逻辑：
        - 唯一意图命中 + 匹配模式数 >= 2 → 高置信 (0.9+)
        - 唯一意图命中 + 匹配模式数 == 1 → 中置信 (0.7)
        - 多意图命中 → 低置信 (0.5)，需 LLM 仲裁
        - 无命中 → 0
        """
        if not rule_type or not rule_scores:
            return 0.0

        num_intents = len(rule_scores)
        hit_count = rule_scores.get(rule_type, 0)

        if num_intents == 1 and hit_count >= 2:
            return 0.95
        if num_intents == 1 and hit_count == 1:
            return 0.75
        if num_intents >= 2:
            # 多意图冲突，置信降低
            return 0.5

        return 0.5

    async def _llm_classify(
        self, query: str, context: str = "", user_id: str | None = None
    ) -> dict[str, Any]:
        if not self.llm:
            return {"query_type": "general_inquiry", "confidence": 0.5, "raw": "no_llm"}

        system_prompt = """你是查询分类专家。将客户查询分类为以下类型之一：
product_info, recommendation, order_status, return_policy, technical_support, usage_guide, complaint, negative_feedback, greeting, general

分类说明：
- product_info: 产品信息查询（价格、成分、功效）
- recommendation: 产品推荐（适合哪种、什么好）
- order_status: 订单状态查询（物流、发货）
- return_policy: 退换货政策（退货、退款、换货）
- technical_support: 技术支持（过敏、刺激、保质期）
- usage_guide: 使用指导（怎么用、使用方法、步骤）
- complaint: 投诉（不满、差评、举报）
- negative_feedback: 负面反馈（失望、太差、不好）
- greeting: 问候（你好、您好）
- general: 其他通用问题

返回 JSON 格式:
{"query_type": "类型", "confidence": 0.0-1.0, "reason": "简要原因"}
只返回 JSON，不要其他内容。"""

        messages = [SystemMessage(content=system_prompt)]
        if context:
            messages.append(HumanMessage(content=f"对话上下文：{context}"))
        if user_id:
            messages.append(HumanMessage(content=query, metadata={"user_id": user_id}))
        else:
            messages.append(HumanMessage(content=query))

        try:
            # 路由使用短超时（LLM_ROUTER_TIMEOUT），避免慢 API 阻塞整个链路
            response = await self.llm.async_invoke(messages, timeout=LLM_ROUTER_TIMEOUT)
            raw = response.content.strip()
            # v3.4: 使用 json.JSONDecoder.raw_decode 替代贪婪正则，更稳健
            try:
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(raw[raw.index("{") :])
                obj["raw"] = raw
                return obj
            except (ValueError, KeyError):
                # 回退：非贪婪正则匹配
                json_match = re.search(r"\{[^{}]*\}", raw)
                if json_match:
                    result = json.loads(json_match.group())
                    result["raw"] = raw
                    return result
            return {"query_type": "general_inquiry", "confidence": 0.3, "raw": raw}
        except Exception as e:
            logger.error(f"LLM 分类失败: {e}", exc_info=True)
            return {"query_type": "general_inquiry", "confidence": 0.1, "raw": "llm_error"}

    def _rule_classify_and_score(
        self, query: str, context: str = ""
    ) -> tuple[str | None, dict[str, int], int]:
        """
        合并规则分类与复杂度评分为单次遍历（v3.3 优化）
        返回 (best_intent, intent_scores, complexity)
        """
        scores: dict[str, int] = {}
        intent_count = 0

        for intent, patterns in _RULE_PATTERNS.items():
            count = sum(1 for p in patterns if p.search(query))
            if count > 0:
                scores[intent] = count
                intent_count += 1

        best_intent = (
            max(scores, key=lambda k: (scores[k], -_INTENT_PRIORITY.get(k, 99))) if scores else None
        )

        # 复杂度评分（复用已计算的 intent_count，避免二次遍历）
        complexity = 0
        if len(query) > 100:
            complexity += 15
        elif len(query) > 50:
            complexity += 10
        elif len(query) > 20:
            complexity += 5

        if intent_count >= 3:
            complexity += 25
        elif intent_count >= 2:
            complexity += 15

        for pattern in _RE_TECH_TERMS:
            if pattern.search(query):
                complexity += 10

        if _RE_PRICE.search(query):
            complexity += 15

        marks = query.count("?") + query.count("？") + query.count("!") + query.count("！")
        complexity += min(marks * 5, 15)

        if context and len(context) > 500:
            complexity += 10

        if best_intent == "complaint":
            complexity += 20

        return best_intent, scores, min(complexity, 100)
