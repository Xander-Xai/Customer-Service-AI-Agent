#!/usr/bin/env python3
"""Agent Eval V1 CLI：在真实图上回放数据集并生成 evidence artifact。

    python3 scripts/evaluate_agent.py                      # 全量
    python3 scripts/evaluate_agent.py --tags hitl          # 只跑某些标签
    python3 scripts/evaluate_agent.py --case hitl_refund_001
    python3 scripts/evaluate_agent.py --print-contract     # 只打印契约与分母

设计纪律
--------
* **离线可重复**：唯一的 LLM 是 :class:`ScriptedAgentLLM` /
  :class:`ScriptedRouterLLM`，不持 API key、不出网。同一份数据集 + 同一份代码
  => 同一份 artifact（容器复用，单次进程内跑完）。
* **不缩小分母**：每个指标都带 numerator / denominator / excluded；分母为 0 时
  输出 ``NOT_MEASURED`` 而不是 0%。失败用例照常进入分母。
* **不自我验证**：数据集里 ``annotation.provenance`` 只有人工确认过的 case
  才会进入 ``route_accuracy`` 分母。用 ``--print-annotation-status`` 看当前
  确认比例 —— 它是 0% 时，路由准确率就是 NOT_MEASURED，这是正确行为。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evaluation.agent_eval import (  # noqa: E402
    AgentEvalHarness,
    DatasetError,
    build_report,
    describe_contract,
    load_dataset,
    write_report,
)


def _git_sha() -> str | None:
    from core.code_provenance import collect_code_provenance

    return collect_code_provenance(REPO_ROOT).commit_sha


def _filter(cases, args):
    if args.case:
        wanted = set(args.case)
        return [c for c in cases if c.case_id in wanted]
    if args.tags:
        wanted = set(args.tags)
        return [c for c in cases if wanted & set(c.tags)]
    return list(cases)


def _print_annotation_status(dataset) -> None:
    counts = dataset.population_counts()
    total = counts["total"] or 1
    print("标注状态（annotation provenance）")
    print(f"  总 case                       : {counts['total']}")
    print(
        f"  human_confirmed               : {counts['human_confirmed']} "
        f"({counts['human_confirmed'] / total:.0%})"
    )
    print(
        f"  llm_candidate（待人工确认）    : {counts['llm_candidate']} "
        f"({counts['llm_candidate'] / total:.0%})"
    )
    print(f"  可进 route_accuracy 分母的 case: {counts['route']}")
    if counts["route"] == 0:
        print(
            "  ⚠️  没有人工确认的 expected_route => route_accuracy 将是 NOT_MEASURED。"
            "\n     这是刻意的：用 LLM 起草的标签验证 LLM 驱动的系统 = 自我验证。\n"
            "     人工确认入口：python3 scripts/approve_agent_eval_annotations.py --help"
        )


async def _run(dataset, cases, hitl_enabled: bool) -> list:
    from evaluation.agent_eval.contract import (  # noqa: F401  (import 供守卫用)
        METRIC_NAMES,
    )

    observations = []
    async with AgentEvalHarness(hitl_enabled=hitl_enabled) as harness:
        for index, case in enumerate(cases, start=1):
            obs = await harness.run_case(case)
            observations.append(obs)
            print(
                f"[{index}/{len(cases)}] {obs.case_id:34s} "
                f"state={obs.terminal_state:16s} route={obs.observed_route} "
                f"mode={obs.observed_mode} tools={list(obs.requested_tool_names)}"
            )
    return observations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--tags", action="append", default=[])
    parser.add_argument("--out", default=None, help="evidence artifact 输出路径")
    parser.add_argument("--print-contract", action="store_true")
    parser.add_argument("--print-annotation-status", action="store_true")
    parser.add_argument("--hitl-disabled", action="store_true")
    args = parser.parse_args(argv)

    if args.print_contract:
        print(json.dumps(describe_contract(), ensure_ascii=False, indent=2))
        return 0

    try:
        dataset = load_dataset(args.dataset)
    except DatasetError as exc:
        print(f"❌ dataset invalid: {exc}", file=sys.stderr)
        return 2

    if args.print_annotation_status:
        _print_annotation_status(dataset)
        return 0

    cases = _filter(dataset.cases, args)
    if not cases:
        print("❌ no cases selected", file=sys.stderr)
        return 2

    print(f"dataset {dataset.path} sha256={dataset.sha256[:16]}… cases={len(dataset)}")
    print(f"running {len(cases)} case(s) on the real compiled graph…\n")
    _print_annotation_status(dataset)
    print()

    observations = asyncio.run(_run(dataset, cases, not args.hitl_disabled))

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = (
        Path(args.out)
        if args.out
        else (REPO_ROOT / "artifacts" / "agent-eval" / stamp / "report.json")
    )

    report = build_report(
        dataset=dataset,
        observations=observations,
        run_id=stamp,
        git_sha=_git_sha(),
        hitl_enabled=not args.hitl_disabled,
        staging_tools_registered=True,
        extra_notes=[f"selected_cases={len(cases)} of {len(dataset)}"],
    )
    report["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report["selection"] = {"selected": len(cases), "dataset_total": len(dataset)}
    from core.code_provenance import collect_code_provenance

    report["code_provenance"] = collect_code_provenance(
        REPO_ROOT, dataset_path=REPO_ROOT / dataset.path
    ).to_dict()
    write_report(report, out)

    print("\n=== 指标 ===")
    for name, metric in sorted(report["metrics"].items()):
        boundary = report["evidence_boundaries"].get(name, {})
        value = "NOT_MEASURED" if metric["value"] is None else f"{metric['value']:.3f}"
        print(
            f"  {name:32s} {value:>14s}  "
            f"n={metric['numerator']:.0f}/{metric['denominator']} "
            f"excluded={metric['excluded']}"
        )
        if boundary.get("does_not_measure"):
            print(f"      ↳ 不证明: {boundary['does_not_measure']}")

    print("\n=== 门禁 ===")
    for name, result in sorted(report["gates"].items()):
        print(f"  {name:32s} {result}")

    print(f"\noverall_status = {report['overall_status']}")
    print(
        f"evidence      -> {out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}"
    )
    return 0 if report["overall_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
