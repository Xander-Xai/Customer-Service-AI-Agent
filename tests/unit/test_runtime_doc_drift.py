"""文档漂移守卫：本轮修复关闭的缺陷，不得再被写成「未修」。

背景
----
上一轮把「`PENDING` 无恢复」「attempt 预算仍有漏洞」「beat 不会自动重启」等已经
处理或已经改变性质的条目留在当前状态章节里，读者会据此得出与代码相反的结论。
这些漂移已经在 `docs/limitations.md` 与 `docs/design/agent-runtime.md` 中改正。

本文件把结论钉住：任何一项若重新变成「未修 / 无恢复 / 未校验」，测试必须失败。
历史证据（曾经是缺陷）可以保留，但**当前状态**不得再断言旧结论。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIMITATIONS = ROOT / "docs" / "limitations.md"
AGENT_RUNTIME = ROOT / "docs" / "design" / "agent-runtime.md"
CONVERGENCE = ROOT / "docs" / "reference" / "runtime-state-convergence.md"
RUNBOOK = ROOT / "docs" / "operations" / "distributed-runtime-runbook.md"

pytestmark = pytest.mark.unit


def _read(path: Path) -> str:
    assert path.exists(), f"文档缺失: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


class TestLimitationsCurrentState:
    """`docs/limitations.md` 的当前状态表格不得再断言旧结论。"""

    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _read(LIMITATIONS)

    def test_pending_is_no_longer_described_as_unrecoverable(self, text):
        assert "PENDING` run 不被 reconciler 覆盖" in text, "该行应保留（作为历史可追溯）"
        row = next(
            line
            for line in text.splitlines()
            if line.startswith("| `PENDING` run 不被 reconciler 覆盖")
        )
        assert "Partial" not in row, "PENDING 已实现条件迁移恢复，不得再标为 Partial"
        assert "Implemented" in row
        assert "永久停在 `PENDING`" not in row, "不得再断言 PENDING 无恢复手段"

    def test_attempt_budget_row_describes_the_atomic_fix_not_the_defective_one(self, text):
        row = next(
            line
            for line in text.splitlines()
            if line.startswith("| attempt 预算在 worker 接管路径上的绕过")
        )
        assert "同在一条 UPDATE 内" in row, "必须描述谓词下沉到单条 UPDATE 的修复"
        assert "无条件 DLQ 写入" in row, (
            "必须保留对**第一版缺陷形态**的说明：Python 侧预算闸门 + 无条件 DLQ 写入。"
            "只写最终形态会让后来者无法判断回归是否复发。"
        )

    def test_fencing_row_claims_cooperative_cancel_but_not_a_token(self, text):
        row = next(
            line
            for line in text.splitlines()
            if line.startswith("| **失去所有权后的执行（fencing）**")
        )
        assert "_FenceGuard" in row and "execution_fence" in row
        assert "取消前已发出的外部调用无法回收" in row, "必须显式承认协作式取消无法回收在途外部调用"
        assert "Partial" in row, "这是 Partial：不是单调 fencing token，不能标成完全实现"

    def test_roadmap_no_longer_lists_completed_items_as_unstarted(self, text):
        for done in (
            "覆盖 `PENDING` 滞留 run",
            "attempt 预算在 worker 接管路径上的闸门",
            # Issue #12 只有 Part B（跨 LLM 轮次 ordinal 重置）算完成；Part A（同类写
            # 重放顺序变化）仍 OPEN，由 test_partially_completed_roadmap_items_stay_open
            # 单独守卫——不得因为 Part B 完成就把整条划掉。
            "非 HITL 副作用工具的 `operation_key` 稳定性（Part B",
            "side-effect claim TTL 早于副作用结束",
            "超时参数交叉校验",
            "强制中止失去所有权的 worker",
        ):
            lines = [ln for ln in text.splitlines() if ln.startswith("|") and done in ln]
            assert lines, f"roadmap 缺少条目: {done}"
            row = lines[0]
            assert "~~" in row and "**已完成**" in row, (
                f"roadmap 条目「{done}」已完成，必须划掉并标注 **已完成**，"
                f"否则读者会以为它仍未开始"
            )
            # 完全完成项必须带完成证据（章节锚点 / 回归用例 / 代码路径之一）。
            assert any(
                marker in row for marker in ("§", "tests/", "core/", "回归")
            ), f"roadmap 条目「{done}」标为完成，必须附完成证据（§ 锚点 / 回归用例 / 代码路径）"

    def test_partially_completed_roadmap_items_stay_open(self, text):
        """部分完成的条目不得被划掉或标成已完成，且必须保留 Issue 引用与真实风险。

        Issue #12 Part A（同类写操作重放顺序变化导致身份漂移 → 对外部系统可能重复执行）
        尚未解决，必须作为独立条目保持可见；不得为了绿灯把它并入已完成的 Part B 或
        写成「已完成」。
        """
        rows = [
            ln
            for ln in text.splitlines()
            if ln.startswith("|") and "operation_key" in ln and "Part A" in ln
        ]
        assert rows, "Issue #12 Part A 必须作为独立 roadmap 条目保留，不得因 Part B 完成而整体划掉"
        row = rows[0]
        assert "~~" not in row, "Part A 仍未解决，不得划掉"
        assert "已完成" not in row, "Part A 仍未解决，不得标注已完成"
        assert "#12" in row, "必须保留 Issue #12 引用，便于追踪未完成项"
        assert "重复执行" in row, "必须保留外部副作用重复执行风险的真实表述"

    def test_beat_row_does_not_claim_no_automatic_restart(self, text):
        """compose 里 beat 有 ``restart: unless-stopped``，旧文案说「不自动重启」是错的。"""
        line = next(
            (ln for ln in text.splitlines() if ln.startswith("| beat 单点")),
            None,
        )
        if line is None:
            pytest.skip("roadmap 已移除 beat 单点条目")
        assert "不自动重启" not in line, (
            "compose 的 beat service 有 restart: unless-stopped；准确表述是「崩溃后会被"
            "拉起，但拉起失败或人工停止期间没有扫描」，且已由 AgentReconcileStalled 覆盖"
        )
        assert "AgentReconcileStalled" in line

    def test_alertmanager_delivery_stays_open(self, text):
        """A5 只验证了 promtool 表达式；通知送达仍必须保持 OPEN。"""
        assert "Alertmanager 通知投递端到端" in text
        line = next(
            ln for ln in text.splitlines() if ln.startswith("| Alertmanager 通知投递端到端")
        )
        assert "未开始" in line or "NOT_VERIFIED" in line or "需可送达" in line


class TestAgentRuntimeDesign:
    def test_operation_key_section_does_not_recommend_the_llm_generated_id(self):
        text = _read(AGENT_RUNTIME)
        assert "build_replay_stable_operation_key" in text
        # 前提条件必须说明非 HITL 不能用 LLM tool_call_id
        assert "不能**用 LLM 生成的 `tool_call_id`" in text or "不能用 LLM 生成的" in text
        assert "去重失效" in text

    def test_hitl_key_semantics_are_still_documented(self):
        text = _read(AGENT_RUNTIME)
        assert "approval:{approval_id}" in text

    def test_no_global_tool_name_arguments_dedup_is_recommended(self):
        text = _read(AGENT_RUNTIME)
        assert "全局去重" in text
        assert "两次合法" in text, "必须说明为什么不能按 tool_name+arguments 全局去重"


class TestConvergenceDoc:
    def test_side_effect_boundary_is_stated_as_a_limit_not_a_guarantee(self):
        text = _read(CONVERGENCE)
        assert "NOT PROVIDED" in text, "端到端 exactly-once 必须保持 NOT PROVIDED"
        assert "下游" in text

    def test_beat_liveness_is_distinguished_from_scan_success(self):
        text = _read(CONVERGENCE)
        assert "beat 存活" in text and "reconciler" in text.lower()
        assert "健康检查" in text

    def test_timeout_table_is_the_executable_mirror(self):
        text = _read(CONVERGENCE)
        for needle in (
            "validate_timeout_invariants",
            "AGENT_RUN_LEASE_SECONDS",
            "AGENT_RUN_VISIBILITY_TIMEOUT",
            "LOCK_TTL",
            "claim TTL",
        ):
            assert needle in text, f"不变量表缺少 {needle}"
        assert (
            "无硬校验" not in text
        ), "lease/heartbeat/visibility 现在有启动校验，不得再写「无硬校验」"


class TestRunbook:
    def test_reconcile_stall_section_exists_with_runbook_anchor(self):
        text = _read(RUNBOOK)
        assert "### 3.8 恢复闭环停滞" in text, "告警的 runbook_url 指向 §3.8，必须存在"
        assert "beat 进程存活 ≠ reconciler 扫描成功" in text

    def test_runbook_does_not_claim_end_to_end_alert_delivery(self):
        text = _read(RUNBOOK)
        assert "不声称已端到端验证" in text or "不验证" in text

    def test_known_limits_reflect_the_new_boundaries(self):
        text = _read(RUNBOOK)
        assert "_FenceGuard" in text, "已知限制必须写清 fencing 的真实边界"
        assert "_ClaimRenewer" in text
        assert "validate_timeout_invariants" in text


class TestOrdinalDriftDocstrings:
    """F3：不得再宣称 ordinal 漂移一定 fail-closed / 不会重复执行。

    ``ordinal`` 是位置不是逻辑操作身份。同一写工具在重放中换槽位会得到新的
    ``operation_key``，``request_fingerprint`` 只在同一个键内比较，因此**可能重复
    执行**。Issue #12 Part A 仍未解决，源码文档不得把它写成已解决。
    """

    @pytest.fixture(scope="class")
    def base_agent_text(self) -> str:
        return (ROOT / "agents" / "base_agent.py").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def tool_registry_text(self) -> str:
        return (ROOT / "tools" / "tool_registry.py").read_text(encoding="utf-8")

    def test_base_agent_does_not_claim_ordinal_drift_cannot_duplicate(self, base_agent_text):
        assert (
            "PermanentError``（fail-closed），不会重复执行" not in base_agent_text
        ), "base_agent.py 不得再宣称 ordinal 漂移一定 fail-closed 不会重复执行"
        assert "可能被再次执行" in base_agent_text or "可能被重复执行" in base_agent_text
        assert "#12" in base_agent_text or "Part A" in base_agent_text

    def test_tool_registry_does_not_claim_ordinal_drift_is_fail_closed(self, tool_registry_text):
        assert (
            "PermanentError``（fail-closed），不会重复执行" not in tool_registry_text
        ), "tool_registry.py 不得再宣称 ordinal 漂移一定 fail-closed 不会重复执行"
        assert "可能被再次执行" in tool_registry_text or "可能被重复执行" in tool_registry_text
        assert "#12" in tool_registry_text or "Part A" in tool_registry_text
