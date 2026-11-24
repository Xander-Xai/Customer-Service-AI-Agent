"""Canonical Routing, Parallel and fixed Supervisor/Workers orchestration.

Legacy node identifiers and event labels are adapters for persisted checkpoints.
ReAct is a single-agent execution strategy, not a fourth orchestration pattern.
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


async def _fan_out(coroutines, timeout):
    """One deadline/cancellation boundary; adopt only children done by the deadline."""
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    done: set[asyncio.Task] = set()
    pending: set[asyncio.Task] = set()
    try:
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for task in tasks:
        if task not in done:
            continue
        if task.cancelled():
            results.append(asyncio.CancelledError())
        elif task.exception() is not None:
            results.append(task.exception())
        else:
            results.append(task.result())
    _raise_write_failures(results)
    return results, bool(pending)


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


class RoutingMode(CollaborationMode):
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


# Compatibility alias for historic imports and checkpoint node names.
SequentialMode = RoutingMode


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
        results, pending = await _fan_out(
            [run_agent(n) for n in agent_names if n in agents],
            self._PARALLEL_TIMEOUT,
        )
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


class SupervisorWorkersMode(CollaborationMode):
    """Fixed worker assignment -> parallel fan-out -> single supervisor synthesis.

    Worker assignments are supplied by application routing, not LLM planning.
    Historic event names and result modes are compatibility policies only.
    """

    async def _supervise(
        self,
        agents,
        state,
        *,
        supervisor,
        workers,
        mode,
        timeout,
    ):
        started = time.time()
        manager = agents.get(supervisor)
        consultation = mode == "consultation"
        sender = f"{mode}_mode"
        if not manager:
            label = "Primary agent" if consultation else "Coordinator"
            return {
                "response": f"{label} {supervisor} not found",
                "mode": mode,
                "agents_used": [],
            }

        if not consultation:
            await self._safe_publish(
                "hierarchical.task.assign",
                sender,
                {
                    "coordinator": supervisor,
                    "sub_tasks": {name: query[:100] for name, query in workers.items()},
                },
            )
        missing_reason = (
            "consultation_agent_missing"
            if consultation
            else "hierarchical_subtask_missing_or_empty"
        )
        failure_reason = (
            missing_reason
            if any(
                name == supervisor or name not in agents or not query.strip()
                for name, query in workers.items()
            )
            else ""
        )

        async def run_worker(name, query):
            worker_state = dict(state)
            worker_state["customer_query"] = query
            if consultation:
                await self._safe_publish(
                    "consultation.consultee.start",
                    sender,
                    {"agent": name, "primary": supervisor},
                )
            else:
                await self._safe_publish(
                    "hierarchical.subtask.start", sender, {"agent": name, "sub_query": query[:100]}
                )
            result = await agents[name].process_with_retry(worker_state)
            response = result.get("response", "")
            if consultation:
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
                try:
                    if self.bus:
                        await self.bus.publish(
                            Message(
                                msg_type=MessageType.RESPONSE,
                                topic=f"consultation.{supervisor}",
                                sender=name,
                                receiver=supervisor,
                                payload={"response": response, "agent": name},
                            )
                        )
                except Exception:
                    logger.debug("Consultation diagnostic event failed", exc_info=True)
                await self._safe_publish(
                    "consultation.consultee.complete",
                    sender,
                    {"agent": name, "primary": supervisor},
                )
            else:
                await self._safe_bb_write(
                    f"hierarchical.subtask.{name}", {"response": response, "sub_query": query}
                )
                await self._safe_publish("hierarchical.subtask.complete", sender, {"agent": name})
            return name, response, result

        results, timed_out = await _fan_out(
            [
                run_worker(name, query)
                for name, query in workers.items()
                if name != supervisor and name in agents and query.strip()
            ],
            timeout,
        )
        if timed_out:
            if _durable_run():
                raise asyncio.TimeoutError(f"{mode} worker deadline exceeded")
            failure_reason = (
                "consultation_timeout" if consultation else "hierarchical_subtask_timeout"
            )

        completed = []
        for item in results:
            if isinstance(item, BaseException):
                failure_reason = failure_reason or (
                    "consultation_agent_failed" if consultation else "hierarchical_subtask_failed"
                )
            else:
                completed.append(item)

        # Synthesis reads only current invocation results, in assignment order.
        enriched = dict(state)
        if completed or not consultation:
            header = "[辅助信息]" if consultation else "[子任务汇总]"
            lines = [
                f"[{name}补充] {response}" if consultation else f"[{name}] {response}"
                for name, response, _ in completed
            ]
            enriched["customer_query"] = (
                f"{state.get('customer_query', '')}\\n\\n{header}\\n" + "\\n".join(lines)
            )

        if consultation:
            await self._safe_publish(
                "consultation.primary.start",
                sender,
                {"agent": supervisor, "consultees": [name for name, _, _ in completed]},
            )
        final = await manager.process_with_retry(enriched)
        elapsed = time.time() - started
        if consultation:
            await self._safe_publish(
                "consultation.primary.complete", sender, {"agent": supervisor, "elapsed": elapsed}
            )

        child_results = [item for _, _, item in completed]
        read_signal = _read_path_signal(final, *child_results)
        if failure_reason:
            read_signal.setdefault("degraded_reason", failure_reason)
        return {
            "response": final.get("response", ""),
            "mode": mode,
            "agents_used": [supervisor] + [name for name, _, _ in completed],
            "elapsed": elapsed,
            **read_signal,
            "pending_actions": [
                action
                for item in [*child_results, final]
                for action in (item.get("pending_actions") or [])
            ],
            "tool_executions": [
                record
                for item in [*child_results, final]
                for record in (item.get("tool_executions") or [])
            ],
        }


class ConsultationMode(SupervisorWorkersMode):
    """Legacy checkpoint adapter: specialist consultation assignment."""

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
        query = f"[辅助请求] 请为以下问题提供专业补充信息：{state.get('customer_query', '')}"
        workers = {name: query for name in context.get("consult_agents", []) if name != primary}
        return await self._supervise(
            agents,
            state,
            supervisor=primary,
            workers=workers,
            mode="consultation",
            timeout=self.consult_timeout,
        )


class HierarchicalMode(SupervisorWorkersMode):
    """Legacy checkpoint adapter: fixed subtask assignment."""

    _HIERARCHICAL_TIMEOUT = SLA_HIERARCHICAL_MAX

    async def execute(
        self, agents: dict[str, Any], state: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._supervise(
            agents,
            state,
            supervisor=context.get("coordinator", "general_agent"),
            workers=context.get("sub_tasks", {}),
            mode="hierarchical",
            timeout=self._HIERARCHICAL_TIMEOUT,
        )


class ReActMode(RoutingMode):
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
