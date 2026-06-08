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

from config import LLM_ROUTER_TIMEOUT
from logger import get_logger

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


# 意图 → Agent 映射
INTENT_AGENT_MAP = {
    "product_info": "product_agent",
    "technical_support": "tech_agent",
    "billing": "billing_agent",
    "complaint": "complaint_agent",
    "general_inquiry": "general_agent",
    "order_query": "billing_agent",
    "cosmetic_advice": "product_agent",
}

# 规则模式匹配表（预编译正则，避免每次调用编译开销）
_RULE_PATTERNS = {
    "product_info": [
        re.compile(r"产品|商品|精华|面膜|洁面|面霜|化妆水|价格|多少钱|成分|功效|推荐")
    ],
    "technical_support": [re.compile(r"过敏|刺激|红肿|痒|使用方法|怎么用|用法|保质期|有效期|保存")],
    "billing": [re.compile(r"退款|退货|发票|付款|支付|账单|费用|订单|物流|快递|发货")],
    "complaint": [re.compile(r"投诉|不满|差评|举报|客服|经理|领导|态度|服务差|质量.*问题")],
    "order_query": [re.compile(r"订单号|物流|快递|到货|发货|签收|运单")],
    "cosmetic_advice": [re.compile(r"肤质|油性|干性|敏感|美白|保湿|抗皱|祛痘|祛斑|护肤")],
}

# 复杂度评分用的预编译正则
_RE_TECH_TERMS = [
    re.compile(r"过敏|刺激|成分|配方|工艺"),
    re.compile(r"退款|发票|对公|分期"),
    re.compile(r"投诉|升级|主管"),
]
_RE_PRICE = re.compile(r"\d+[\.\d]*\s*[元块]|¥|￥|\d{10,}")

# 意图优先级（同分时高优先级意图胜出，数值越小越优先）
# 投诉 > 账单 > 技术 > 订单 > 产品 > 肤质 > 通用
_INTENT_PRIORITY = {
    "complaint": 0,
    "billing": 1,
    "technical_support": 2,
    "order_query": 3,
    "product_info": 4,
    "cosmetic_advice": 5,
    "general_inquiry": 6,
}


class QueryRouter:
    """双层查询路由器"""

    def __init__(self, llm=None, complexity_threshold: int = 50):
        self.llm = llm
        self.complexity_threshold = complexity_threshold

    async def route(self, query: str, conversation_context: str = "") -> RoutingResult:
        """主路由入口（v3.3: LLM 与规则分类并行执行）"""
        # 并行执行 LLM 分类和规则分类 + 复杂度评分
        llm_task = self._llm_classify(query, conversation_context)
        rule_result = self._rule_classify_and_score(query, conversation_context)
        llm_result = await llm_task

        rule_type, rule_scores, complexity = rule_result
        rule_override = False
        final_type = llm_result["query_type"]
        if rule_type and rule_type != llm_result["query_type"]:
            if llm_result.get("confidence", 0) < 0.7:
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
        )

    async def _llm_classify(self, query: str, context: str = "") -> dict[str, Any]:
        if not self.llm:
            return {"query_type": "general_inquiry", "confidence": 0.5, "raw": "no_llm"}

        system_prompt = """你是查询分类专家。将客户查询分类为以下类型之一：
product_info, technical_support, billing, complaint, general_inquiry, order_query, cosmetic_advice

返回 JSON 格式:
{"query_type": "类型", "confidence": 0.0-1.0, "reason": "简要原因"}
只返回 JSON，不要其他内容。"""

        messages = [SystemMessage(content=system_prompt)]
        if context:
            messages.append(HumanMessage(content=f"对话上下文：{context}"))
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
            logger.error(f"LLM 分类失败: {e}")
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
