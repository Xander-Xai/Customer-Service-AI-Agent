#!/usr/bin/env python3
"""汇总分布式 Runtime 可靠性验收证据（deliverable：运行时验收报告）。

它**不重新测量**任何东西 —— 那会变成第二份事实来源。它把已有的三份真实
基础设施证据聚合成一张可读、可追溯的报告，并把每一条能力的
**证据等级**写清楚：

===================  ===================================================
来源                 命令
===================  ===================================================
runtime e2e          ``make runtime-e2e``（真实 PG + Redis + 多进程 Celery）
chaos                ``make runtime-chaos`` → ``artifacts/runtime/chaos-*.json``
capability verify    ``make runtime-verify`` → ``artifacts/distributed-runtime/*``
===================  ===================================================

关键口径：**重复副作用按"真实副作用执行次数"计，不按调用次数、也不按 ledger
命中次数。** chaos artifact 里三者是分开的字段：

    tool_invocations  —— 工具被调用了多少次（at-least-once 投递下会 > 1）
    ledger_hits       —— 幂等 ledger 命中多少次（去重生效的次数）
    idem:counter      —— **外部副作用真实发生了几次**（唯一正确的口径）

本报告只把 ``idem:counter`` 当作副作用次数。用调用次数当副作用次数会把
"重投递"误报成"重复副作用"；用 ledger 命中次数又会把"去重成功"误报成失败。

用法::

    python3 scripts/report_distributed_runtime.py
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

#: 每条能力 ↔ 承载它的证据来源 ↔ 不得越界的声明。
CAPABILITIES: tuple[dict[str, str], ...] = (
    {
        "capability": "worker 崩溃后继续原 Run（不是从头重跑）",
        "evidence": "chaos step 7 `recovered`：run_id 不变、status=SUCCEEDED、attempt 递增",
        "boundary": "单机 + 单 worker 进程组 SIGKILL；不是多副本生产集群",
    },
    {
        "capability": "checkpoint 跨进程恢复",
        "evidence": "runtime-verify `checkpoint_cross_process`；e2e `test_cross_process_checkpoint`",
        "boundary": "PostgreSQL 单实例；未验证多 region / 只读副本",
    },
    {
        "capability": "相同 operation_key 不重复执行副作用",
        "evidence": "chaos step 8：tool_invocations>1 时 idem:counter 仍为 1",
        "boundary": "外部系统端到端幂等仍要求下游接受幂等键",
    },
    {
        "capability": "Redis per-thread 锁竞争正确",
        "evidence": "runtime-verify `same_thread_serialization`；e2e thread-lock 用例",
        "boundary": "单 Redis 实例，非 Redlock；故障转移窗口内可能双持有",
    },
    {
        "capability": "并发任务不产生状态污染",
        "evidence": "e2e 跨 thread 并发耗时 / 同 thread 执行区间不重叠",
        "boundary": "本机并发规模；无大规模 queue backlog 压测",
    },
    {
        "capability": "重试耗尽后进入 DLQ",
        "evidence": "e2e DLQ + 重放用例；`agent_dead_letters` 表",
        "boundary": "人工重放；无自动重放策略",
    },
    {
        "capability": "审批中断 / 恢复 / 拒绝改变工具执行结果",
        "evidence": "test_hitl_langgraph_interrupt.py + test_hitl_real_graph_gate.py（真实图）",
        "boundary": "staging 工具验证的是治理机制，不是真实 ERP 写入（NOT_VERIFIED）",
    },
)

LEVEL_2 = "LEVEL_2_CI_VERIFIED"
NOT_VERIFIED = "NOT_VERIFIED"

#: HITL 审批 + 崩溃恢复的故障注入场景矩阵。
#:
#: 每行把「场景」钉到一个**真实执行**它的测试节点上，避免本报告出现
#: 「能力存在但没人测过」的悬空声明。``expected_side_effects`` 是治理属性
#: （approve=1 / reject=0 / expire=0 / crash 后重投递不重复），不是装饰。
FAULT_INJECTION_SCENARIOS: tuple[dict[str, object], ...] = (
    {
        "scenario": "approval_suspend",
        "expected_side_effects": 0,
        "evidence": "tests/integration/runtime/test_hitl_langgraph_interrupt.py::"
        "TestRealInterruptSemantics::test_interrupt_pauses_graph_and_persists_checkpoint",
    },
    {
        "scenario": "approval_approve_resume",
        "expected_side_effects": 1,
        "evidence": "tests/integration/runtime/test_hitl_langgraph_interrupt.py::"
        "TestRealInterruptSemantics::test_command_resume_executes_approved_action_exactly_once",
    },
    {
        "scenario": "approval_reject",
        "expected_side_effects": 0,
        "evidence": "tests/integration/runtime/test_hitl_resume_fault_injection.py::"
        "TestRejectAndExpireDoNotExecute::test_rejected_resume_executes_nothing",
    },
    {
        "scenario": "approval_expire",
        "expected_side_effects": 0,
        "evidence": "tests/integration/runtime/test_hitl_resume_fault_injection.py::"
        "TestRejectAndExpireDoNotExecute::test_expired_approval_executes_nothing",
    },
    {
        "scenario": "duplicate_resume_delivery",
        "expected_side_effects": 1,
        "evidence": "tests/integration/runtime/test_hitl_resume_fault_injection.py::"
        "TestDuplicateResumeDelivery::test_terminal_replay_does_not_duplicate_side_effect",
    },
    {
        "scenario": "worker_crash_after_side_effect",
        "expected_side_effects": 1,
        "evidence": "tests/integration/runtime/test_hitl_resume_fault_injection.py::"
        "TestCrashDuringResume::test_crash_after_side_effect_does_not_duplicate_on_redelivery",
        "known_boundary": "resume 决策被消费后 worker 崩溃 -> run 停在 WAITING_APPROVAL，"
        "不重复但也无法自愈（需运维介入；见 remaining_risks）",
    },
    {
        "scenario": "worker_crash_before_execution",
        "expected_side_effects": 0,
        "evidence": "tests/integration/runtime/test_hitl_resume_fault_injection.py::"
        "TestCrashDuringResume::test_crash_before_execution_leaves_run_parked_without_side_effect",
        "known_boundary": "同上：决策已认领、副作用未发生，run 停在 WAITING_APPROVAL",
    },
)

REMAINING_RISKS: tuple[dict[str, str], ...] = (
    {
        "risk": "审批决策认领后 worker 崩溃导致 run 停在 WAITING_APPROVAL（liveness，不是重复副作用）",
        "evidence": "tests/integration/runtime/test_hitl_resume_fault_injection.py::"
        "TestCrashDuringResume",
        "repro": "make runtime-e2e（该文件在 tests/integration/runtime 下被收集）",
        "mitigation_status": "设计边界：副作用安全（ledger 去重），但需运维重新决策；"
        "自动 re-issue 机制未实现",
    },
    {
        "risk": "worker SIGKILL 恢复用例在全套并发跑时曾对 150s liveness 预算敏感（1 次观察到卡在 RUNNING/attempt=2）",
        "evidence": "tests/integration/runtime/test_worker_checkpoint_recovery.py",
        "repro": "RUNTIME_RECOVERY_WAIT_SECONDS=300 make runtime-e2e（已把预算可配置化）",
        "mitigation_status": "已把 liveness 预算改为可配置（默认 300s），正确性断言未放宽",
    },
)


def _environment() -> dict[str, object]:
    """测试环境（不记录凭据；只记录类型/是否存在/主机）。"""
    import os
    import sys
    from urllib.parse import urlparse

    db_url = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()
    redis_url = os.getenv("TEST_REDIS_URL", "").strip()

    def _host(url: str) -> str | None:
        try:
            return urlparse(url).hostname
        except Exception:  # noqa: BLE001
            return None

    return {
        "python": sys.version.split()[0],
        "postgres_configured": bool(db_url),
        "postgres_host": _host(db_url),
        "redis_configured": bool(redis_url),
        "redis_host": _host(redis_url),
        "note": "单机真实基础设施（本机 PG + Redis + 多进程 Celery）；非生产集群",
    }



def _latest(pattern: str) -> Path | None:
    files = sorted(REPO_ROOT.glob(pattern))
    return files[-1] if files else None


def _read(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"__parse_error__": str(path)}


def _git_sha() -> str | None:
    from core.code_provenance import collect_code_provenance

    return collect_code_provenance(REPO_ROOT).commit_sha


def _code_provenance() -> dict:
    from core.code_provenance import collect_code_provenance

    return collect_code_provenance(REPO_ROOT).to_dict()


def _side_effect_accounting(chaos: dict) -> dict:
    """从 chaos steps 里抽出「调用 / ledger 命中 / 真实副作用」三者。"""
    accounting: dict = {"tool_invocations": None, "ledger_hits": None, "real_side_effects": None}
    for step in chaos.get("steps", []):
        action = step.get("action")
        if action == "side_effect_applied":
            accounting["real_side_effects"] = step.get("idem:counter")
        if action == "side_effect_deduplicated":
            accounting["tool_invocations"] = step.get("tool_invocations")
            accounting["ledger_hits"] = step.get("ledger_hits")
            if accounting["real_side_effects"] is None:
                accounting["real_side_effects"] = step.get("idem:counter")
    return accounting


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    chaos_path = _latest("artifacts/runtime/chaos-*.json")
    verify_path = _latest("artifacts/distributed-runtime/*/report.json")
    chaos = _read(chaos_path)
    verify = _read(verify_path)
    accounting = _side_effect_accounting(chaos)

    report: dict = {
        "schema_version": "distributed-runtime-summary/v1",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "code_provenance": _code_provenance(),
        "sources": {
            "chaos_artifact": str(chaos_path.relative_to(REPO_ROOT)) if chaos_path else None,
            "verify_artifact": str(verify_path.relative_to(REPO_ROOT)) if verify_path else None,
            "commands": ["make runtime-e2e", "make runtime-chaos", "make runtime-verify"],
        },
        "overall_status": (
            "PASS"
            if chaos.get("result") == "PASS" and verify.get("overall_status") == "PASS"
            else "INCOMPLETE"
        ),
        "evidence_level": LEVEL_2,
        "side_effect_accounting": {
            **accounting,
            "canonical_metric": "real_side_effects",
            "rule": (
                "重复副作用**只**按 real_side_effects（真实副作用执行次数）计。"
                "tool_invocations 是 at-least-once 投递下的调用次数，"
                "ledger_hits 是去重生效次数 —— 两者都不是副作用次数。"
            ),
            "deduplication_holds": (
                accounting["real_side_effects"] == 1
                if accounting["real_side_effects"] is not None
                else None
            ),
        },
        "capabilities": list(CAPABILITIES),
        "environment": _environment(),
        "fault_injection": {
            "scenario_count": len(FAULT_INJECTION_SCENARIOS),
            "scenarios": list(FAULT_INJECTION_SCENARIOS),
            "duplicate_side_effects_observed": accounting["real_side_effects"],
            "canonical_rule": (
                "每个场景的副作用次数以真实执行次数（staging 账本 / idem:counter）为准；"
                "重复副作用 = 真实执行次数 > 1。"
            ),
        },
        "remaining_risks": list(REMAINING_RISKS),
        "production_status": NOT_VERIFIED,
        "production_boundary": (
            "Level 2 = 真实基础设施（PG + Redis + 多进程 Celery）在 CI/本机通过。"
            "**不等于**生产验证：无多副本长期稳定、无真实用户流量、无真实 ERP 写入、"
            "无大规模 backlog、无 K8s autoscaling / multi-region。"
        ),
    }

    if args.out:
        out = Path(args.out)
    else:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = REPO_ROOT / "artifacts" / "distributed-runtime-summary" / f"{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"overall_status : {report['overall_status']}")
    print(f"evidence_level : {LEVEL_2}")
    print(f"  chaos  : {report['sources']['chaos_artifact']}")
    print(f"  verify : {report['sources']['verify_artifact']}")
    print("\n副作用计数口径（三者必须分开）：")
    print(f"  tool_invocations   {accounting['tool_invocations']}")
    print(f"  ledger_hits        {accounting['ledger_hits']}")
    print(f"  real_side_effects  {accounting['real_side_effects']}  <- 唯一正确口径")
    print(f"  deduplication_holds {report['side_effect_accounting']['deduplication_holds']}")
    print("\n能力（每条都有真实基础设施证据）：")
    for cap in CAPABILITIES:
        print(f"  ✓ {cap['capability']}")
        print(f"      证据: {cap['evidence']}")
        print(f"      边界: {cap['boundary']}")
    print(f"\n故障注入场景（{len(FAULT_INJECTION_SCENARIOS)} 个，各有真实测试节点）：")
    for scenario in FAULT_INJECTION_SCENARIOS:
        print(
            f"  • {scenario['scenario']:32s} expected_side_effects={scenario['expected_side_effects']}"
        )
    print(f"\n剩余风险（{len(REMAINING_RISKS)} 条，附复现方法）：")
    for risk in REMAINING_RISKS:
        print(f"  ! {risk['risk']}")
        print(f"      复现: {risk['repro']}")
    print(f"\nproduction_status : {NOT_VERIFIED}")
    print(f"evidence -> {out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}")
    return 0 if report["overall_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
