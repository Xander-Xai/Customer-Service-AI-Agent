#!/usr/bin/env python3
"""Agent Eval 回归比较器：把两次 eval artifact 的差异变成**可判定的结论**。

    python3 scripts/compare_agent_eval_regression.py --baseline <a.json> --current <b.json>
    python3 scripts/compare_agent_eval_regression.py --baseline <a.json> --current <b.json> \
        --out artifacts/agent-eval-regression/<ts>/report.json

为什么需要它
------------
仓库里一直有 ``make agent-eval``（跑一次、出一份 artifact）和 CI 里的
``agent-eval`` job（门禁不过就红）。但这两者都只能回答"**现在**过不过"，
回答不了工业交付里最要紧的那个问题：

    **"这次改动有没有让能力退步？"**

没有基线比较，一次 ``task_completion_rate`` 从 0.96 掉到 0.94，在只看
"当前是否仍高于 0.95 阈值"的门禁下**完全不会报警**——它还在阈值之上，
于是静默退化被放行。这正是"回归评测（Regression Eval）"在阶段 10 与阶段 12
的职责：把两次运行放在一起，逐指标判定方向。

三条设计纪律
------------
1. **只比可比的**。artifact 的 schema 版本或数据集 sha256 不一致时，
   直接判为 ``INCOMPARABLE`` 并**拒绝**给出结论。跨口径的数字相减
   （例如 v1.2 的 evidence_coverage 与 v1.3 的）没有意义 —— 契约本身
   就声明过"三代数字互不可比"。
2. **方向来自门禁契约，不来自猜测**。``gte`` 类指标跌了是回归，``lte``
   类指标涨了是回归。比较器从 artifact 自带的 ``gates`` 契约读方向，
   缺门禁的指标（如 ``human_intervention_rate``）只报数值变化、
   **不判定回归** —— 负载信号涨跌不是能力退化。
3. **NOT_MEASURED 不是 0**。基线里测不出来的指标，当前测出来了（或反过来）
   是"**可测性变化**"，单独报告，不算能力回归；两边都测不出来则跳过。
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

#: 比较结论（三态，与 agent-eval 的三态门禁同构）
REGRESSION = "REGRESSION"
IMPROVEMENT = "IMPROVEMENT"
UNCHANGED = "UNCHANGED"
INCOMPARABLE = "INCOMPARABLE"

#: 判定"变化是否值得报告"的最小绝对差。低于它算 UNCHANGED ——
#: 否则 0.999999 → 1.0 这种浮点噪声会天天报警。
DEFAULT_EPSILON = 1e-9

#: 比较器**不判定**回归、只报告数值变化的指标及原因。
UNGATED_METRICS = {
    "human_intervention_rate": "人工介入率是负载信号，不是质量分；涨跌不构成能力回归。",
    "response_delivery_rate": "交付率几乎恒为 1（交付兜底文案也算交付），退化会被完成率捕获。",
    "fallback_rate": "降级率是输入属性（故障注入用例决定），不独立反映能力。",
    "rule_route_agreement": "诊断指标：规则分类器与标注的一致率，不是端到端能力。",
    "expected_parameter_match_rate": "诊断指标：参数端到端保真度，不是模型能力。",
    "task_completion_evidence_coverage": "证据覆盖率的绝对值受数据集构成影响过大，只报变化。",
    "step_count": "步数是诊断量，不是质量分。",
}


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"❌ artifact not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"❌ artifact is not valid JSON: {path} ({exc})") from None


def _metric_value(artifact: dict[str, Any], name: str) -> tuple[float | None, str]:
    """取一个指标的 (值, 状态)。缺失指标按 NOT_MEASURED 处理。"""
    metrics = artifact.get("metrics") or {}
    entry = metrics.get(name)
    if not isinstance(entry, dict):
        return None, "NOT_MEASURED"
    status = str(entry.get("status") or "NOT_MEASURED")
    value = entry.get("value")
    if status != "MEASURED" or not isinstance(value, int | float) or isinstance(value, bool):
        return None, status
    return float(value), status


def _direction(gate_spec: dict[str, Any]) -> str:
    op = str(gate_spec.get("op") or "")
    if op == "gte":
        return "up"
    if op == "lte":
        return "down"
    return "none"


def compare_artifacts(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    epsilon: float = DEFAULT_EPSILON,
) -> dict[str, Any]:
    """比较两份 artifact，产出结构化回归结论。"""
    base_schema = baseline.get("schema_version")
    cur_schema = current.get("schema_version")
    base_ds = (baseline.get("dataset") or {}).get("sha256")
    cur_ds = (current.get("dataset") or {}).get("sha256")

    incomparability: list[str] = []
    if base_schema != cur_schema:
        incomparability.append(
            f"artifact schema differs: baseline={base_schema!r} current={cur_schema!r} "
            "(metric semantics changed; the numbers are not comparable)"
        )
    if not isinstance(base_ds, str) or not base_ds.strip():
        incomparability.append(
            "baseline dataset sha256 is missing; denominator identity is unknown"
        )
    if not isinstance(cur_ds, str) or not cur_ds.strip():
        incomparability.append("current dataset sha256 is missing; denominator identity is unknown")
    if (
        isinstance(base_ds, str)
        and base_ds.strip()
        and isinstance(cur_ds, str)
        and cur_ds.strip()
        and base_ds != cur_ds
    ):
        incomparability.append(
            f"dataset sha256 differs: baseline={base_ds[:16]}… current={cur_ds[:16]}… "
            "(different denominators; the numbers are not comparable)"
        )

    for label, artifact in (("baseline", baseline), ("current", current)):
        provenance = artifact.get("code_provenance")
        if (
            not isinstance(provenance, dict)
            or provenance.get("publication_status") != "REPRODUCIBLE"
            or provenance.get("dirty") is not False
        ):
            incomparability.append(
                f"{label} code provenance is not reproducible; only clean committed "
                "artifacts can be used for a regression verdict"
            )

    # 门禁契约从 **artifact 自带的** ``gate_specs`` 读（op + threshold）。
    # 刻意**不** import 线上 GATES 常量：比较历史 artifact 时，方向必须来自
    # 那次运行当时记录的契约，而不是当前代码 —— 否则"改了门禁阈值"会
    # 悄悄改写历史 artifact 的解读。旧 artifact 没有该段时退化为
    # "无方向"，此时只报数值变化、不判定回归。
    gates = current.get("gate_specs")
    if not isinstance(gates, dict):
        gates = {}
    contract_gates = current.get("contract") or {}

    metric_names = sorted(set(baseline.get("metrics") or {}) | set(current.get("metrics") or {}))

    changes: list[dict[str, Any]] = []
    regressions: list[str] = []
    improvements: list[str] = []

    for name in metric_names:
        base_value, base_status = _metric_value(baseline, name)
        cur_value, cur_status = _metric_value(current, name)
        spec = gates.get(name)
        if not isinstance(spec, dict):
            spec = None

        entry: dict[str, Any] = {
            "metric": name,
            "baseline_value": base_value,
            "current_value": cur_value,
            "baseline_status": base_status,
            "current_status": cur_status,
            "gated": spec is not None,
            "direction": _direction(spec) if spec else "none",
            "verdict": UNCHANGED,
            "delta": None,
            "reason": None,
        }

        if base_value is None and cur_value is None:
            entry["reason"] = "not measurable in either run; nothing to compare"
            entry["verdict"] = UNCHANGED
            changes.append(entry)
            continue

        if base_value is None or cur_value is None:
            # 可测性变化：不是能力回归，单独标注。
            entry["verdict"] = UNCHANGED
            entry["reason"] = (
                "measurability changed (metric was NOT_MEASURED in one run); "
                "this is a coverage change, not a capability regression"
            )
            changes.append(entry)
            continue

        delta = cur_value - base_value
        entry["delta"] = delta
        if abs(delta) <= epsilon:
            entry["verdict"] = UNCHANGED
            entry["reason"] = "value unchanged within epsilon"
            changes.append(entry)
            continue

        if spec is None:
            entry["verdict"] = UNCHANGED
            entry["reason"] = UNGATED_METRICS.get(
                name, "metric is not gated; a value change is reported but not judged"
            )
            changes.append(entry)
            continue

        direction = entry["direction"]
        if direction == "none":
            entry["verdict"] = UNCHANGED
            entry["reason"] = "gate op is not directional; change reported but not judged"
        elif direction == "up":
            if delta < 0:
                entry["verdict"] = REGRESSION
                regressions.append(name)
            else:
                entry["verdict"] = IMPROVEMENT
                improvements.append(name)
        else:  # "down" —— 越低越好
            if delta > 0:
                entry["verdict"] = REGRESSION
                regressions.append(name)
            else:
                entry["verdict"] = IMPROVEMENT
                improvements.append(name)
        changes.append(entry)

    if incomparability:
        overall = INCOMPARABLE
        # 不可比时**不保留**回归/改进名单：口径不同的两个数字相减没有意义，
        # 把它们列进 "regressed_metrics" 会让读者以为这是一次确凿的退步。
        # 逐指标的数值变化仍然留在 ``changes`` 里，供人工阅读。
        regressions = []
        improvements = []
        for entry in changes:
            entry["verdict"] = INCOMPARABLE
            entry["reason"] = "artifacts are not comparable; no capability judgement is made"
    elif regressions:
        overall = REGRESSION
    elif improvements:
        overall = IMPROVEMENT
    else:
        overall = UNCHANGED

    return {
        "schema_version": "agent-eval-regression/v1",
        "generated_at": None,
        "overall": overall,
        "baseline": {
            "schema_version": base_schema,
            "dataset_sha256": base_ds,
            "overall_status": baseline.get("overall_status"),
            "git_sha": baseline.get("git_sha"),
        },
        "current": {
            "schema_version": cur_schema,
            "dataset_sha256": cur_ds,
            "overall_status": current.get("overall_status"),
            "git_sha": current.get("git_sha"),
        },
        "incomparability": incomparability,
        "regressed_metrics": regressions,
        "improved_metrics": improvements,
        "changes": changes,
        "notes": [
            "comparability requires the same artifact schema AND the same dataset sha256; "
            "otherwise the verdict is INCOMPARABLE and no capability claim is made",
            "regression direction comes from the gate contract (gte = up is good, "
            "lte = down is good); ungated metrics are reported but never judged",
            "a metric that is NOT_MEASURED in one run and MEASURED in the other is a "
            "coverage change, not a capability regression",
            "this comparator compares the SAME scripted-LLM lane only; a scripted run "
            "and a real-provider run must never be compared against each other",
        ],
        "contract_gates": contract_gates.get("metric_names") or [],
    }


def _print_summary(report: dict[str, Any], baseline_path: Path, current_path: Path) -> None:
    print(f"baseline: {baseline_path}")
    print(f"current : {current_path}")
    print(f"\noverall: {report['overall']}")
    if report["incomparability"]:
        print("\n⚠️  不可比较（因此不给能力结论）：")
        for reason in report["incomparability"]:
            print(f"   - {reason}")
    judged = [c for c in report["changes"] if c["verdict"] in (REGRESSION, IMPROVEMENT)]
    if judged:
        print("\n=== 判定为变化 ===")
        for change in judged:
            print(
                f"  {change['metric']:34s} {change['verdict']:12s} "
                f"{change['baseline_value']} -> {change['current_value']} "
                f"(delta={change['delta']:+.6f})"
            )
    else:
        print("\n（没有指标被判定为回归或改进）")
    unjudged = [c for c in report["changes"] if c["gated"] is False and c["delta"] is not None]
    if unjudged:
        print("\n=== 数值变化但不判定（无门禁 / 负载信号）===")
        for change in unjudged:
            print(
                f"  {change['metric']:34s} {change['baseline_value']} -> "
                f"{change['current_value']} — {change['reason']}"
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="baseline agent-eval artifact JSON")
    parser.add_argument("--current", required=True, help="current agent-eval artifact JSON")
    parser.add_argument("--out", default=None, help="regression report output path")
    parser.add_argument(
        "--epsilon",
        type=float,
        default=DEFAULT_EPSILON,
        help="minimum absolute delta to treat as a change (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    baseline_path = Path(args.baseline)
    current_path = Path(args.current)
    baseline = _load(baseline_path)
    current = _load(current_path)

    report = compare_artifacts(baseline, current, epsilon=args.epsilon)
    report["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    out = Path(args.out) if args.out else None
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_summary(report, baseline_path, current_path)
    if out is not None:
        print(f"\nreport -> {out}")

    # CI must fail closed on an unusable comparison as well as a measured
    # regression. Treating INCOMPARABLE as exit 0 would turn missing provenance
    # or a changed denominator into a silent green gate.
    if report["overall"] == REGRESSION:
        return 1
    if report["overall"] == INCOMPARABLE:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
