"""
4+1 种协作模式实现（v4.3: 独立 SLA 超时 + ReAct 推理模式）
核心改造：
- 移除 asyncio.to_thread，直接 await Agent（消除死锁风险）
- 集成 MessageBus 事件发布 + SharedBlackboard 数据共享
- 结构化日志
- v3.5: ReActMode 支持 RAG + Function Calling 自主推理
- v4.3: 各模式独立 SLA 超时配置
"""

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from config import SLA_CONSULTATION_MAX, SLA_HIERARCHICAL_MAX, SLA_PARALLEL_MAX
from core.message_bus import Message, MessageBus, MessageType
from core.shared_blackboard import SharedBlackboard
from logger import get_logger

logger = get_logger("collaboration.modes")


class CollaborationMode(ABC):
    """协作模式基类"""

    def __init__(self, bus: MessageBus | None = None, bb: SharedBlackboard | None = None):
        self.bus = bus
        self.bb = bb

    @abstractmethod
    async def execute(
        self, agents: dict[str, Any], state: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        """执行协作"""

    async def _safe_publish(self, topic: str, sender: str, payload: dict):
        """安全发布 MessageBus 事件（静默失败，debug 日志含异常详情）"""
        try:
            if self.bus:
                await self.bus.publish(
                    Message(
                        msg_type=MessageType.BROADCAST,
                        topic=topic,
                        sender=sender,
                        payload=payload,
                    )
                )
        except Exception as e:
            logger.debug(f"{sender}: 发布 {topic} 事件失败: {e}")

    async def _safe_bb_write(self, key: str, value: Any, ttl: float = 300.0):
        """安全写入 SharedBlackboard（静默失败，debug 日志含异常详情）"""
        try:
            if self.bb:
                await self.bb.write(key, value, ttl=ttl)
        except Exception as e:
            logger.debug(f"写入 Blackboard 失败 ({key}): {e}")

    async def _safe_bb_read_prefix(self, prefix: str) -> dict:
        """安全读取 SharedBlackboard 前缀数据（静默失败，debug 日志含异常详情）"""
        try:
            if self.bb:
                return await self.bb.read_prefix(prefix)
        except Exception as e:
            logger.debug(f"读取 Blackboard 失败 ({prefix}*): {e}")
        return {}


class SequentialMode(CollaborationMode):
    """顺序模式：单个 Agent 处理"""

    async def execute(
        self, agents: dict[str, Any], state: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        agent_name = context.get("primary_agent", "general_agent")
        agent = agents.get(agent_name)
        if not agent:
            return {
                "response": f"Agent {agent_name} not found",
                "mode": "sequential",
                "agents_used": [],
            }

        start = time.time()

        await self._safe_publish(
            "agent.start", "sequential_mode", {"agent": agent_name, "mode": "sequential"}
        )

        result = await agent.process_with_retry(dict(state))
        elapsed = time.time() - start

        await self._safe_publish(
            "agent.complete",
            "sequential_mode",
            {"agent": agent_name, "mode": "sequential", "elapsed": elapsed},
        )

        logger.info(f"Sequential {agent_name} {elapsed:.1f}s")
        return {
            "response": result.get("response", ""),
            "mode": "sequential",
            "agents_used": [agent_name],
            "elapsed": elapsed,
        }


class ParallelMode(CollaborationMode):
    """并行模式：多 Agent 同时处理 + 结果聚合（v3.4: Semaphore 限流，v3.6: 超时保护）"""

    _PARALLEL_TIMEOUT = SLA_PARALLEL_MAX  # v4.3: 使用配置化超时

    def __init__(
        self,
        max_workers: int = 5,
        bus: MessageBus | None = None,
        bb: SharedBlackboard | None = None,
    ):
        super().__init__(bus=bus, bb=bb)
        self.max_workers = max_workers
        self._semaphore: asyncio.Semaphore | None = None  # v3.4: 懒初始化

    def _get_semaphore(self) -> asyncio.Semaphore:
        """v3.4: 懒初始化信号量，限制并行 Agent 数量"""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_workers)
        return self._semaphore

    async def execute(
        self, agents: dict[str, Any], state: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        agent_names = context.get("agent_list", [])
        start = time.time()
        sem = self._get_semaphore()  # v3.4: 获取信号量

        async def run_agent(name: str):
            async with sem:  # v3.4: 限制并行数
                agent = agents.get(name)
                if not agent:
                    return name, f"[{name}] Agent not found", 0
                s = time.time()

                await self._safe_publish(
                    "agent.start", "parallel_mode", {"agent": name, "mode": "parallel"}
                )

                result = await agent.process_with_retry(dict(state))
                elapsed = time.time() - s

                await self._safe_bb_write(
                    f"parallel.result.{name}",
                    {"response": result.get("response", ""), "elapsed": elapsed},
                )

                await self._safe_publish(
                    "agent.complete",
                    "parallel_mode",
                    {"agent": name, "mode": "parallel", "elapsed": elapsed},
                )

                return name, result.get("response", ""), elapsed

        tasks = [run_agent(n) for n in agent_names if n in agents]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self._PARALLEL_TIMEOUT,  # v3.6: 超时保护
            )
        except asyncio.TimeoutError:
            logger.warning(f"Parallel 执行超时 ({self._PARALLEL_TIMEOUT}s)")
            results = []

        responses = []
        agents_used = []
        for r in results:
            if isinstance(r, Exception):
                responses.append(f"[error] {r}")
            else:
                name, resp, _ = r
                responses.append(f"【{name}】\n{resp}")
                agents_used.append(name)

        aggregated = "\n\n---\n\n".join(responses) if responses else "无可用 Agent 响应"
        elapsed = time.time() - start

        await self._safe_publish(
            "parallel.complete", "parallel_mode", {"agents_used": agents_used, "elapsed": elapsed}
        )

        logger.info(f"Parallel agents={agents_used} {elapsed:.1f}s")
        return {
            "response": aggregated,
            "mode": "parallel",
            "agents_used": agents_used,
            "elapsed": elapsed,
        }


class ConsultationMode(CollaborationMode):
    """咨询模式：主 Agent 处理 + 向辅助 Agent 请求补充信息"""

    def __init__(
        self,
        consult_timeout: float = SLA_CONSULTATION_MAX,
        bus: MessageBus | None = None,
        bb: SharedBlackboard | None = None,
    ):
        super().__init__(bus=bus, bb=bb)
        self.consult_timeout = consult_timeout

    async def execute(
        self, agents: dict[str, Any], state: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        primary = context.get("primary_agent", "general_agent")
        consultees = context.get("consult_agents", [])
        start = time.time()

        primary_agent = agents.get(primary)
        if not primary_agent:
            return {
                "response": f"Primary agent {primary} not found",
                "mode": "consultation",
                "agents_used": [],
            }

        # 先让辅助 Agent 提供信息
        consult_results = []
        if consultees:

            async def consult(name: str):
                agent = agents.get(name)
                if not agent:
                    return name, ""
                consult_state = dict(state)
                consult_state["customer_query"] = (
                    f"[辅助请求] 请为以下问题提供专业补充信息：{state.get('customer_query', '')}"
                )

                await self._safe_publish(
                    "consultation.consultee.start",
                    "consultation_mode",
                    {"agent": name, "primary": primary},
                )

                result = await agent.process_with_retry(consult_state)
                response = result.get("response", "")

                prefix = (
                    "tech."
                    if "tech" in name
                    else "erp."
                    if "billing" in name
                    else f"consult.{name}."
                )
                await self._safe_bb_write(
                    f"{prefix}consult_result", {"agent": name, "response": response}
                )

                # 通过 Bus request 模式向主 Agent 发送补充信息
                try:
                    if self.bus:
                        await self.bus.publish(
                            Message(
                                msg_type=MessageType.RESPONSE,
                                topic=f"consultation.{primary}",
                                sender=name,
                                receiver=primary,
                                payload={"response": response, "agent": name},
                            )
                        )
                except Exception as e:
                    logger.debug(f"Consultation: 发送补充信息失败 ({name} -> {primary}): {e}")

                await self._safe_publish(
                    "consultation.consultee.complete",
                    "consultation_mode",
                    {"agent": name, "primary": primary},
                )

                return name, response

            tasks = [consult(n) for n in consultees if n in agents]
            try:
                done = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self.consult_timeout,  # v3.6: 强制超时，防止慢 Agent 阻塞
                )
            except asyncio.TimeoutError:
                logger.warning(f"Consultation 辅助 Agent 超时 ({self.consult_timeout}s)")
                done = []
            for r in done:
                if not isinstance(r, Exception):
                    consult_results.append(r)

        # 将辅助信息注入主 Agent 上下文
        enriched_state = dict(state)
        if consult_results:
            consult_text = "\n".join([f"[{name}补充] {resp}" for name, resp in consult_results])
            enriched_state["customer_query"] = (
                f"{state.get('customer_query', '')}\n\n[辅助信息]\n{consult_text}"
            )

        # 从 Blackboard 读取辅助 Agent 写入的 erp.*、tech.* 数据作为补充
        erp_data = await self._safe_bb_read_prefix("erp.")
        tech_data = await self._safe_bb_read_prefix("tech.")
        bb_context = []
        for key, val in {**erp_data, **tech_data}.items():
            if isinstance(val, dict) and val.get("response"):
                bb_context.append(f"[{key}] {val['response'][:300]}")
        if bb_context:
            enriched_state["customer_query"] += "\n\n[Blackboard 参考]\n" + "\n".join(bb_context)

        await self._safe_publish(
            "consultation.primary.start",
            "consultation_mode",
            {"agent": primary, "consultees": [n for n, _ in consult_results]},
        )

        result = await primary_agent.process_with_retry(enriched_state)
        elapsed = time.time() - start

        await self._safe_publish(
            "consultation.primary.complete",
            "consultation_mode",
            {"agent": primary, "elapsed": elapsed},
        )

        logger.info(f"Consultation primary={primary} {elapsed:.1f}s")
        return {
            "response": result.get("response", ""),
            "mode": "consultation",
            "agents_used": [primary] + [n for n, _ in consult_results],
            "elapsed": elapsed,
        }


class HierarchicalMode(CollaborationMode):
    """层次模式：协调者分配子任务给多个 Agent，汇总后输出（v3.6: 超时保护）"""

    _HIERARCHICAL_TIMEOUT = SLA_HIERARCHICAL_MAX  # v4.3: 使用配置化超时

    async def execute(
        self, agents: dict[str, Any], state: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        coordinator_name = context.get("coordinator", "general_agent")
        sub_tasks = context.get("sub_tasks", {})
        start = time.time()

        coordinator = agents.get(coordinator_name)
        if not coordinator:
            return {
                "response": f"Coordinator {coordinator_name} not found",
                "mode": "hierarchical",
                "agents_used": [],
            }

        # 协调者通过 Bus 广播子任务分配
        await self._safe_publish(
            "hierarchical.task.assign",
            "hierarchical_mode",
            {
                "coordinator": coordinator_name,
                "sub_tasks": {n: q[:100] for n, q in sub_tasks.items()},
            },
        )

        # 并行执行子任务
        async def run_subtask(name: str, sub_query: str):
            agent = agents.get(name)
            if not agent:
                return name, ""

            await self._safe_publish(
                "hierarchical.subtask.start",
                "hierarchical_mode",
                {"agent": name, "sub_query": sub_query[:100]},
            )

            sub_state = dict(state)
            sub_state["customer_query"] = sub_query
            result = await agent.process_with_retry(sub_state)
            response = result.get("response", "")

            await self._safe_bb_write(
                f"hierarchical.subtask.{name}", {"response": response, "sub_query": sub_query}
            )

            await self._safe_publish(
                "hierarchical.subtask.complete", "hierarchical_mode", {"agent": name}
            )

            return name, response

        tasks = [run_subtask(n, q) for n, q in sub_tasks.items() if n in agents and q.strip()]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self._HIERARCHICAL_TIMEOUT,  # v3.6: 超时保护
            )
        except asyncio.TimeoutError:
            logger.warning(f"Hierarchical 子任务执行超时 ({self._HIERARCHICAL_TIMEOUT}s)")
            results = []

        sub_responses = []
        agents_used = []
        for r in results:
            if isinstance(r, Exception):
                continue
            name, resp = r
            sub_responses.append(f"[{name}] {resp}")
            agents_used.append(name)

        # 从 Blackboard 读取所有子任务结果作为汇总补充
        bb_subtask_data = await self._safe_bb_read_prefix("hierarchical.subtask.")
        for key, data in bb_subtask_data.items():
            agent_tag = key.replace("hierarchical.subtask.", "")
            if agent_tag not in agents_used and isinstance(data, dict) and data.get("response"):
                sub_responses.append(f"[{agent_tag}(BB)] {data['response'][:300]}")

        # 协调者汇总
        summary_state = dict(state)
        summary_state["customer_query"] = (
            f"{state.get('customer_query', '')}\n\n[子任务汇总]\n" + "\n".join(sub_responses)
        )
        final = await coordinator.process_with_retry(summary_state)
        elapsed = time.time() - start

        logger.info(f"Hierarchical sub_tasks={list(sub_tasks.keys())} {elapsed:.1f}s")
        return {
            "response": final.get("response", ""),
            "mode": "hierarchical",
            "agents_used": [coordinator_name] + agents_used,
            "elapsed": elapsed,
        }


class ReActMode(SequentialMode):
    """ReAct 推理模式（v3.5）：复用 SequentialMode 流程，仅覆盖 Agent 选择逻辑"""

    async def execute(
        self, agents: dict[str, Any], state: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        agent_name = "react_agent"
        agent = agents.get(agent_name)

        if not agent:
            primary = context.get("primary_agent", "general_agent")
            agent = agents.get(primary)
            if not agent:
                return {"response": "无可用 Agent", "mode": "react", "agents_used": []}
            agent_name = primary
            logger.warning(f"ReActAgent 未注册，回退到 {primary}")

        start = time.time()
        await self._safe_publish(
            "agent.start", "react_mode", {"agent": agent_name, "mode": "react"}
        )
        result = await agent.process_with_retry(dict(state))
        elapsed = time.time() - start
        await self._safe_publish(
            "agent.complete",
            "react_mode",
            {"agent": agent_name, "mode": "react", "elapsed": elapsed},
        )
        logger.info(f"ReAct {agent_name} {elapsed:.1f}s")
        return {
            "response": result.get("response", ""),
            "mode": "react",
            "agents_used": [agent_name],
            "elapsed": elapsed,
        }
