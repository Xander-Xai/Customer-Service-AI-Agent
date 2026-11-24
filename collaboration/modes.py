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

from core.config import (
    SLA_CONSULTATION_MAX,
    SLA_HIERARCHICAL_MAX,
    SLA_PARALLEL_MAX,
    SLA_REACT_MAX,
    SLA_SEQUENTIAL_MAX,
)
from core.logger import get_logger
from core.message_bus import Message, MessageBus, MessageType
from core.shared_blackboard import SharedBlackboard
from tools.tool_registry import SideEffectExecutionError

logger = get_logger("collaboration.modes")


def _read_path_signal(*results: dict[str, Any]) -> dict[str, str]:
    """Propagate read-path degradation from nested per-Agent state copies."""
    for result in results:
        if isinstance(result, dict) and result.get("erp_read_status") in ("error", "timeout"):
            return {
                "erp_read_status": str(result["erp_read_status"]),
                "degraded_reason": str(result.get("degraded_reason") or "erp_read_error"),
            }
    for result in results:
        if isinstance(result, dict) and result.get("degraded_reason"):
            return {"degraded_reason": str(result["degraded_reason"])}
    return {}


def _durable_run() -> bool:
    from runtime.context import get_current_run_id

    return bool(get_current_run_id())


def _raise_write_failures(results: list) -> None:
    """Never aggregate a failed durable write into a successful answer."""
    for result in results:
        if isinstance(result, SideEffectExecutionError):
            raise result


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
    """Direct routed single specialist; legacy sequential node identifier."""

    _SEQUENTIAL_TIMEOUT = SLA_SEQUENTIAL_MAX

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

        return await self._execute_single(
            agent,
            agent_name,
            state,
            mode="sequential",
            timeout=self._SEQUENTIAL_TIMEOUT,
            timeout_reply="抱歉，响应时间过长，请稍后重试或简化您的提问。",
        )

    async def _execute_single(
        self, agent, agent_name, state, *, mode, timeout, timeout_reply
    ) -> dict[str, Any]:
        """Common single-agent execution boundary; mode selects policy, not another loop."""
        start = time.time()
        sender = f"{mode}_mode"
        await self._safe_publish("agent.start", sender, {"agent": agent_name, "mode": mode})
        try:
            result = await asyncio.wait_for(agent.process_with_retry(dict(state)), timeout=timeout)
        except asyncio.TimeoutError:
            if _durable_run():
                raise
            logger.warning(f"{mode} {agent_name} 超时 ({timeout}s)，返回降级回复")
            return {
                "response": timeout_reply,
                "mode": mode,
                "agents_used": [agent_name],
                "elapsed": time.time() - start,
            }
        elapsed = time.time() - start
        await self._safe_publish(
            "agent.complete", sender, {"agent": agent_name, "mode": mode, "elapsed": elapsed}
        )
        logger.info(f"{mode} {agent_name} {elapsed:.1f}s")
        return {
            "response": result.get("response", ""),
            "mode": mode,
            "agents_used": [agent_name],
            "elapsed": elapsed,
            **_read_path_signal(result),
            # Preserve actual receipts and proposed high-risk actions across copied state.
            "pending_actions": list(result.get("pending_actions") or []),
            "tool_executions": list(result.get("tool_executions") or []),
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
        agent_names = list(dict.fromkeys(context.get("agent_list", [])))
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

                return name, result.get("response", ""), result

        failure_reason = (
            "parallel_agent_missing" if any(n not in agents for n in agent_names) else ""
        )
        tasks = [asyncio.create_task(run_agent(n)) for n in agent_names if n in agents]
        done: set[asyncio.Task] = set()
        pending: set[asyncio.Task] = set()
        try:
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=self._PARALLEL_TIMEOUT)
        finally:
            # Drain all children on deadline or caller cancellation. Never adopt
            # a late result from a child that had not completed by the deadline.
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        results = []
        for task in tasks:
            if task in done:
                if task.cancelled():
                    results.append(asyncio.CancelledError())
                elif task.exception() is not None:
                    results.append(task.exception())
                else:
                    results.append(task.result())

        _raise_write_failures(results)
        if pending:
            if _durable_run():
                raise asyncio.TimeoutError("parallel agent deadline exceeded")
            logger.warning(f"Parallel 执行超时 ({self._PARALLEL_TIMEOUT}s)")
            failure_reason = "parallel_timeout"
        responses = []
        agents_used = []
        collected_pending: list[dict] = []
        collected_tool_executions: list[dict] = []
        read_path_results: list[dict] = []
        for r in results:
            if isinstance(r, BaseException):
                failure_reason = failure_reason or "parallel_agent_failed"
                responses.append("[error] 部分 Agent 暂时不可用")
            else:
                name, resp, agent_result = r
                responses.append(f"【{name}】\n{resp}")
                agents_used.append(name)
                collected_pending.extend(agent_result.get("pending_actions") or [])
                collected_tool_executions.extend(agent_result.get("tool_executions") or [])
                read_path_results.append(agent_result)

        aggregated = "\n\n---\n\n".join(responses) if responses else "无可用 Agent 响应"
        elapsed = time.time() - start

        await self._safe_publish(
            "parallel.complete", "parallel_mode", {"agents_used": agents_used, "elapsed": elapsed}
        )

        logger.info(f"Parallel agents={agents_used} {elapsed:.1f}s")
        read_signal = _read_path_signal(*read_path_results)
        if failure_reason:
            read_signal.setdefault("degraded_reason", failure_reason)
        return {
            "response": aggregated,
            "mode": "parallel",
            "agents_used": agents_used,
            "elapsed": elapsed,
            **read_signal,
            # human-in-the-loop：透传 Agent 工具循环摘出的 HIGH 风险动作。
            # 少了这一行，被摘出的退款/改单会停在 Agent 内部**静默丢弃** ——
            # 既没执行、也没审批，而 run 照常报告成功。当前只有 react 模式持有
            # tool_registry，因此只有它会真的摘出动作；其余模式一并透传是为了
            # 让「模式返回值漏字段」这一整类 bug 不再有存活空间。
            "pending_actions": collected_pending,
            # 业务结果证据：透传**真实执行过**的工具调用，供 core.outcome 区分
            # "交付了回答" 与 "业务动作确实执行了"。漏传会让证据在编排层消失。
            "tool_executions": collected_tool_executions,
        }


class ConsultationMode(CollaborationMode):
    """Fixed specialist fan-out followed by primary synthesis (manager-style)."""

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
        consultees = [
            name for name in dict.fromkeys(context.get("consult_agents", [])) if name != primary
        ]
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
        consult_read_results: list[dict] = []
        consult_failure_reason = (
            "consultation_agent_missing" if any(n not in agents for n in consultees) else ""
        )
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
                consult_read_results.append(result)
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
                if _durable_run():
                    raise
                logger.warning(f"Consultation 辅助 Agent 超时 ({self.consult_timeout}s)")
                consult_failure_reason = "consultation_timeout"
                done = []
            _raise_write_failures(done)
            for r in done:
                if isinstance(r, BaseException):
                    consult_failure_reason = consult_failure_reason or "consultation_agent_failed"
                else:
                    consult_results.append(r)

        # 将辅助信息注入主 Agent 上下文
        enriched_state = dict(state)
        if consult_results:
            consult_text = "\n".join([f"[{name}补充] {resp}" for name, resp in consult_results])
            enriched_state["customer_query"] = (
                f"{state.get('customer_query', '')}\n\n[辅助信息]\n{consult_text}"
            )

        # Only current invocation results are synthesis input. Session blackboard
        # entries have no turn receipt and can survive earlier or concurrent turns.

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
        # Preserve only execution-produced structured records, not inferred facts
        # from the auxiliary response or Blackboard text.
        pending_actions = [
            action
            for item in consult_read_results
            for action in (item.get("pending_actions") or [])
        ]
        tool_executions = [
            record
            for item in consult_read_results
            for record in (item.get("tool_executions") or [])
        ]
        read_signal = _read_path_signal(result, *consult_read_results)
        if consult_failure_reason:
            read_signal.setdefault("degraded_reason", consult_failure_reason)
        return {
            "response": result.get("response", ""),
            "mode": "consultation",
            "agents_used": [primary] + [n for n, _ in consult_results],
            "elapsed": elapsed,
            **read_signal,
            # human-in-the-loop：透传 Agent 工具循环摘出的 HIGH 风险动作。
            # 少了这一行，被摘出的退款/改单会停在 Agent 内部**静默丢弃** ——
            # 既没执行、也没审批，而 run 照常报告成功。当前只有 react 模式持有
            # tool_registry，因此只有它会真的摘出动作；其余模式一并透传是为了
            # 让「模式返回值漏字段」这一整类 bug 不再有存活空间。
            "pending_actions": pending_actions + list(result.get("pending_actions") or []),
            # 业务结果证据：透传**真实执行过**的工具调用，供 core.outcome 区分
            # "交付了回答" 与 "业务动作确实执行了"。漏传会让证据在编排层消失。
            "tool_executions": tool_executions + list(result.get("tool_executions") or []),
        }


class HierarchicalMode(CollaborationMode):
    """Fixed subtask fan-out and coordinator synthesis; not dynamic LLM planning."""

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

        # Keep execution-produced receipts even if a later sibling times out.
        completed_subtasks: list[tuple[str, str, dict[str, Any]]] = []
        subtask_failure_reason = (
            "hierarchical_subtask_missing_or_empty"
            if any(n not in agents or not q.strip() for n, q in sub_tasks.items())
            else ""
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
            completed_subtasks.append((name, response, result))

            await self._safe_bb_write(
                f"hierarchical.subtask.{name}", {"response": response, "sub_query": sub_query}
            )

            await self._safe_publish(
                "hierarchical.subtask.complete", "hierarchical_mode", {"agent": name}
            )

            return name, response, result

        tasks = [run_subtask(n, q) for n, q in sub_tasks.items() if n in agents and q.strip()]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self._HIERARCHICAL_TIMEOUT,  # v3.6: 超时保护
            )
        except asyncio.TimeoutError:
            if _durable_run():
                raise
            logger.warning(f"Hierarchical 子任务执行超时 ({self._HIERARCHICAL_TIMEOUT}s)")
            subtask_failure_reason = "hierarchical_subtask_timeout"
            results = completed_subtasks

        _raise_write_failures(results)
        sub_responses = []
        agents_used = []
        collected_pending: list[dict] = []
        collected_tool_executions: list[dict] = []
        read_path_results: list[dict] = []
        for r in results:
            if isinstance(r, BaseException):
                subtask_failure_reason = subtask_failure_reason or "hierarchical_subtask_failed"
                continue
            name, resp, agent_result = r
            sub_responses.append(f"[{name}] {resp}")
            agents_used.append(name)
            collected_pending.extend(agent_result.get("pending_actions") or [])
            collected_tool_executions.extend(agent_result.get("tool_executions") or [])
            read_path_results.append(agent_result)

        # Do not recover missing subtask text from session-level diagnostics.
        # Only completed results from this invocation may contribute evidence.

        # 协调者汇总
        summary_state = dict(state)
        summary_state["customer_query"] = (
            f"{state.get('customer_query', '')}\n\n[子任务汇总]\n" + "\n".join(sub_responses)
        )
        final = await coordinator.process_with_retry(summary_state)
        elapsed = time.time() - start

        read_signal = _read_path_signal(final, *read_path_results)
        if subtask_failure_reason:
            read_signal.setdefault("degraded_reason", subtask_failure_reason)
        logger.info(f"Hierarchical sub_tasks={list(sub_tasks.keys())} {elapsed:.1f}s")
        return {
            "response": final.get("response", ""),
            "mode": "hierarchical",
            "agents_used": [coordinator_name] + agents_used,
            "elapsed": elapsed,
            **read_signal,
            # human-in-the-loop：透传 Agent 工具循环摘出的 HIGH 风险动作。
            # 少了这一行，被摘出的退款/改单会停在 Agent 内部**静默丢弃** ——
            # 既没执行、也没审批，而 run 照常报告成功。当前只有 react 模式持有
            # tool_registry，因此只有它会真的摘出动作；其余模式一并透传是为了
            # 让「模式返回值漏字段」这一整类 bug 不再有存活空间。
            "pending_actions": collected_pending + list(final.get("pending_actions") or []),
            # 业务结果证据：透传**真实执行过**的工具调用，供 core.outcome 区分
            # "交付了回答" 与 "业务动作确实执行了"。漏传会让证据在编排层消失。
            "tool_executions": collected_tool_executions + list(final.get("tool_executions") or []),
        }


class ReActMode(SequentialMode):
    """ReAct 推理模式（v3.5→v6.3: 添加 SLA 超时保护）
    复用 SequentialMode 流程，仅覆盖 Agent 选择逻辑。
    """

    _REACT_TIMEOUT = SLA_REACT_MAX

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

        return await self._execute_single(
            agent,
            agent_name,
            state,
            mode="react",
            timeout=self._REACT_TIMEOUT,
            timeout_reply="抱歉，推理过程耗时过长，请稍后重试或简化您的提问。",
        )
