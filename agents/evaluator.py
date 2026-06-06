"""
回答质量评估器（v4.1）
基于用户反馈 + 规则评分，自动评估 Agent 回答质量。
实现自我评估闭环：评估 -> 反馈聚合 -> 低分告警 -> Prompt 优化依据
"""
import re
import time
from typing import Dict, Any, List, Optional
from logger import get_logger

logger = get_logger("agents.evaluator")

# ===== 礼貌用语词库 =====
_POLITE_PHRASES = [
    "您好", "你好", "感谢", "谢谢", "请", "很高兴",
    "祝您", "祝你", "如有", "若有", "如有任何", "欢迎",
    "不客气", "乐意", "帮您", "帮您解决",
]

# ===== 冗余/低质量特征 =====
_FILLER_PHRASES = [
    "让我", "我来", "首先", "让我分析", "让我想想",
    "我需要", "我来帮你", "根据我的分析", "基于我的分析",
]

# ===== 问题覆盖度关键词模式（按类别）=====
_COVERAGE_KEYWORDS = {
    "complaint": ["处理", "解决", "赔偿", "退款", "补偿", "方案"],
    "product": ["参数", "规格", "功能", "特点", "配置", "价格"],
    "tech": ["操作", "步骤", "方法", "设置", "安装", "故障"],
    "billing": ["费用", "账单", "金额", "支付", "充值", "扣费"],
}


class ResponseEvaluator:
    """
    回答质量评估器（v4.1）
    基于多维度规则评分，评估 Agent 回答质量。
    返回综合评分(0-100)、各维度因子、改进建议。
    """

    def evaluate(self, response: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        评估回答质量

        Args:
            response: Agent 回答内容
            context: 上下文信息
                - query: 用户原始问题
                - query_type: 问题类型（complaint/product/tech/billing/general）
                - resolution_status: 解决状态
                - cached: 是否缓存命中

        Returns:
            {"score": 0-100, "factors": {...}, "suggestions": [...]}
        """
        if not response or not response.strip():
            return {
                "score": 0,
                "factors": {"completeness": 0, "accuracy": 0, "conciseness": 0,
                            "politeness": 0, "relevance": 0},
                "suggestions": ["回答为空，需要重新生成"],
            }

        query = context.get("query", "")
        query_type = context.get("query_type", "general")

        # 五个维度评分
        completeness = self._score_completeness(response, query, query_type)
        accuracy = self._score_accuracy(response, context)
        conciseness = self._score_conciseness(response, query)
        politeness = self._score_politeness(response)
        relevance = self._score_relevance(response, query)

        factors = {
            "completeness": completeness,
            "accuracy": accuracy,
            "conciseness": conciseness,
            "politeness": politeness,
            "relevance": relevance,
        }

        # 加权综合评分
        weights = {
            "completeness": 0.25,
            "accuracy": 0.25,
            "conciseness": 0.15,
            "politeness": 0.10,
            "relevance": 0.25,
        }
        score = sum(factors[k] * weights[k] for k in factors)
        score = round(min(100, max(0, score)), 1)

        # 生成改进建议
        suggestions = self._generate_suggestions(factors, response, query)

        result = {
            "score": score,
            "factors": factors,
            "suggestions": suggestions,
        }

        logger.debug(
            f"评估完成: score={score} factors={factors} "
            f"query_type={query_type} response_len={len(response)}"
        )
        return result

    def aggregate_feedback(self, session_feedbacks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        聚合用户反馈，计算满意度

        Args:
            session_feedbacks: 反馈列表，每项包含:
                - rating: 1=点赞, -1=点踩
                - score: 评估器评分（可选）
                - timestamp: 时间戳

        Returns:
            {
                "total": 总反馈数,
                "positive": 点赞数,
                "negative": 点踩数,
                "satisfaction_rate": 满意度 (0.0-1.0),
                "avg_score": 平均评估分,
                "trend": 趋势 ("improving" / "declining" / "stable"),
            }
        """
        if not session_feedbacks:
            return {
                "total": 0, "positive": 0, "negative": 0,
                "satisfaction_rate": 0.0, "avg_score": 0.0, "trend": "stable",
            }

        total = len(session_feedbacks)
        positive = sum(1 for f in session_feedbacks if f.get("rating", 0) > 0)
        negative = sum(1 for f in session_feedbacks if f.get("rating", 0) < 0)

        satisfaction_rate = positive / total if total > 0 else 0.0

        scores = [f["score"] for f in session_feedbacks if "score" in f]
        avg_score = sum(scores) / len(scores) if scores else 0.0

        # 趋势判断：后半段 vs 前半段满意度
        trend = self._compute_trend(session_feedbacks)

        return {
            "total": total,
            "positive": positive,
            "negative": negative,
            "satisfaction_rate": round(satisfaction_rate, 3),
            "avg_score": round(avg_score, 1),
            "trend": trend,
        }

    # ----- 各维度评分方法 -----

    def _score_completeness(self, response: str, query: str, query_type: str) -> float:
        """
        完整性评分 (0-100)
        评估回答是否覆盖了问题的多个方面。
        """
        score = 50.0  # 基准分

        # 回答长度合理性（太短扣分，太长也轻微扣分）
        resp_len = len(response.strip())
        if resp_len < 20:
            score -= 30
        elif resp_len < 50:
            score -= 15
        elif resp_len > 1000:
            score -= 5  # 过长但不至于严重

        # 分点回答加分（有结构化内容）
        if re.search(r"[1-9][.、）)]\s|[-*]\s|•\s", response):
            score += 10

        # 包含对问题类型的覆盖关键词
        expected_keywords = _COVERAGE_KEYWORDS.get(query_type, [])
        if expected_keywords:
            keyword_hits = sum(1 for kw in expected_keywords if kw in response)
            coverage_ratio = keyword_hits / len(expected_keywords)
            score += coverage_ratio * 20  # 最多加 20 分

        # 回答与问题的词汇重叠度
        if query:
            query_chars = set(query)
            resp_chars = set(response)
            overlap = len(query_chars & resp_chars) / max(len(query_chars), 1)
            score += overlap * 10  # 最多加 10 分

        return min(100, max(0, score))

    def _score_accuracy(self, response: str, context: Dict[str, Any]) -> float:
        """
        准确性评分 (0-100)
        基于回答是否包含知识库引用、数据准确性等启发式判断。
        """
        score = 60.0  # 基准分

        # 包含知识库引用标记加分
        if "[知识库]" in response or "根据" in response or "根据系统记录" in response:
            score += 15

        # 包含具体数据/数字加分（价格、订单号等）
        if re.search(r"\d{4,}", response):
            score += 5

        # 包含确定性表达加分
        certain_phrases = ["已确认", "查询结果", "系统显示", "记录显示", "当前状态"]
        certain_hits = sum(1 for p in certain_phrases if p in response)
        score += min(certain_hits * 3, 10)

        # 不确定性表达扣分
        uncertain_phrases = [
            "可能", "大概", "也许", "不太确定", "我猜测",
            "我不确定", "建议您咨询", "建议您联系",
        ]
        uncertain_hits = sum(1 for p in uncertain_phrases if p in response)
        score -= uncertain_hits * 5

        # 回答中包含错误降级标记扣分
        if response in ("处理出错，请重试", "处理出错"):
            score -= 40

        return min(100, max(0, score))

    def _score_conciseness(self, response: str, query: str) -> float:
        """
        简洁性评分 (0-100)
        评估回答是否过于冗长或信息密度低。
        """
        resp_len = len(response.strip())

        # 理想长度范围：50-500 字符
        if 50 <= resp_len <= 500:
            score = 90.0
        elif resp_len < 50:
            score = 70.0  # 偏短，但不一定差
        elif resp_len <= 800:
            score = 75.0
        elif resp_len <= 1500:
            score = 55.0
        else:
            score = 35.0  # 过长

        # 检查填充词比例（信息密度）
        filler_count = sum(response.count(f) for f in _FILLER_PHRASES)
        filler_ratio = filler_count / max(resp_len / 10, 1)
        if filler_ratio > 0.3:
            score -= 15
        elif filler_ratio > 0.15:
            score -= 8

        # 检查重复内容
        sentences = re.split(r"[。！？\n]", response)
        sentences = [s.strip() for s in sentences if s.strip()]
        if sentences:
            unique_ratio = len(set(sentences)) / len(sentences)
            if unique_ratio < 0.7:
                score -= 15  # 大量重复内容

        return min(100, max(0, score))

    def _score_politeness(self, response: str) -> float:
        """
        礼貌性评分 (0-100)
        评估回答是否使用了礼貌用语和友好语气。
        """
        score = 50.0  # 基准分

        polite_hits = sum(1 for p in _POLITE_PHRASES if p in response)
        score += min(polite_hits * 8, 40)  # 最多加 40 分

        # 语气友好加分
        friendly_endings = ["吗？", "呢？", "哦", "哈", "哟"]
        if any(response.rstrip().endswith(e) for e in friendly_endings):
            score += 5

        # 无礼貌用语但也不粗鲁
        if polite_hits == 0:
            score = max(score, 40.0)  # 至少 40 分

        return min(100, max(0, score))

    def _score_relevance(self, response: str, query: str) -> float:
        """
        相关性评分 (0-100)
        评估回答是否与问题相关。
        """
        if not query:
            return 60.0  # 无问题时给基准分

        score = 40.0  # 基准分

        # 词汇重叠度
        query_chars = set(re.findall(r"[一-鿿]+", query))
        resp_chars = set(re.findall(r"[一-鿿]+", response))
        if query_chars:
            overlap = len(query_chars & resp_chars) / len(query_chars)
            score += overlap * 40  # 最多加 40 分

        # 回答包含完整问题关键词的短语
        query_words = re.findall(r"[一-鿿]{2,}", query)
        phrase_hits = sum(1 for w in query_words if w in response)
        if query_words:
            phrase_ratio = phrase_hits / len(query_words)
            score += phrase_ratio * 20  # 最多加 20 分

        return min(100, max(0, score))

    def _generate_suggestions(self, factors: Dict[str, float],
                              response: str, query: str) -> List[str]:
        """根据各维度评分生成改进建议"""
        suggestions = []

        if factors["completeness"] < 60:
            suggestions.append("回答不够完整，建议覆盖问题的更多方面")
        if factors["accuracy"] < 60:
            suggestions.append("回答准确性偏低，建议引用知识库或系统数据")
        if factors["conciseness"] < 50:
            suggestions.append("回答过于冗长，建议精简内容、提高信息密度")
        if factors["politeness"] < 50:
            suggestions.append("建议增加礼貌用语，提升用户体验")
        if factors["relevance"] < 60:
            suggestions.append("回答与问题关联度不够，建议紧扣用户问题")

        # 特定问题诊断
        if len(response.strip()) < 15:
            suggestions.append("回答过短（<15字符），可能未有效回答问题")
        if response in ("处理出错，请重试", "处理出错"):
            suggestions.append("回答为错误降级响应，需要排查上游异常")

        return suggestions

    def _compute_trend(self, feedbacks: List[Dict[str, Any]]) -> str:
        """计算反馈趋势：improving / declining / stable"""
        if len(feedbacks) < 4:
            return "stable"

        sorted_feedbacks = sorted(feedbacks, key=lambda f: f.get("timestamp", 0))
        mid = len(sorted_feedbacks) // 2

        first_half = sorted_feedbacks[:mid]
        second_half = sorted_feedbacks[mid:]

        def _satisfaction_half(fb_list):
            if not fb_list:
                return 0.5
            pos = sum(1 for f in fb_list if f.get("rating", 0) > 0)
            return pos / len(fb_list)

        first_sat = _satisfaction_half(first_half)
        second_sat = _satisfaction_half(second_half)

        diff = second_sat - first_sat
        if diff > 0.1:
            return "improving"
        elif diff < -0.1:
            return "declining"
        return "stable"

    # ===== P2-4: LLM-as-Judge 评估 =====

    async def evaluate_with_llm(self, query: str, response: str, llm_client) -> Dict[str, Any]:
        """
        P2-4: 使用 LLM 对客服回复进行五维度评分。
        与规则评估互补，提供更准确的质量判断。

        Args:
            query: 用户原始问题
            response: Agent 回答
            llm_client: OpenAICompatibleClient 实例

        Returns:
            {"score": 0-100, "factors": {...}, "raw_evaluation": str}
        """
        prompt = f"""你是一个专业的客服质量评估专家。请对以下客服回复进行五维度评分（每个维度 0-100 分）。

## 用户问题
{query}

## 客服回复
{response}

## 评分维度
1. completeness（完整性）：回复是否全面覆盖了用户问题的各个方面
2. accuracy（准确性）：回复中的信息是否准确、专业
3. conciseness（简洁性）：回复是否简洁明了，无冗余内容
4. politeness（礼貌性）：回复语气是否友好、专业
5. relevance（相关性）：回复是否紧扣用户问题，无偏题

请严格按以下 JSON 格式返回（不要输出其他内容）：
{{"completeness": 分数, "accuracy": 分数, "conciseness": 分数, "politeness": 分数, "relevance": 分数}}"""

        try:
            from langchain_core.messages import HumanMessage
            result = await llm_client.async_invoke([HumanMessage(content=prompt)])
            raw = result.content.strip()

            # 解析 JSON 响应
            import json
            # 尝试提取 JSON（LLM 可能输出 markdown 包裹的 JSON）
            json_match = re.search(r'\{[^}]+\}', raw)
            if json_match:
                factors = json.loads(json_match.group())
                # 验证所有维度都存在且在 0-100 范围内
                required_dims = ["completeness", "accuracy", "conciseness", "politeness", "relevance"]
                for dim in required_dims:
                    if dim not in factors:
                        factors[dim] = 50.0
                    factors[dim] = min(100, max(0, float(factors[dim])))

                weights = {
                    "completeness": 0.25, "accuracy": 0.25, "conciseness": 0.15,
                    "politeness": 0.10, "relevance": 0.25,
                }
                score = round(sum(factors[k] * weights[k] for k in factors), 1)

                logger.debug(f"LLM-as-Judge 评估完成: score={score} factors={factors}")
                return {
                    "score": score,
                    "factors": factors,
                    "raw_evaluation": raw,
                    "method": "llm_judge",
                }
            else:
                logger.warning(f"LLM-as-Judge 响应格式异常: {raw[:200]}")
                return {"score": 0, "factors": {}, "raw_evaluation": raw, "method": "llm_judge_error"}

        except Exception as e:
            logger.warning(f"LLM-as-Judge 评估失败: {e}")
            return {"score": 0, "factors": {}, "raw_evaluation": str(e), "method": "llm_judge_error"}
