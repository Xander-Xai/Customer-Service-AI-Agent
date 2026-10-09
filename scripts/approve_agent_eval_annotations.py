#!/usr/bin/env python3
"""人工确认 Agent Eval 数据集的标注（route_accuracy 分母的唯一入口）。

为什么必须有人工这一步
--------------------
``agent_cases.jsonl`` 里每条 case 的 ``annotation.provenance`` 默认是
``llm_candidate``：标签由 LLM 依据 ``router/query_router.py`` 的意图分类契约
起草，**没有**被任何人看过。在这种状态下：

* ``expected_route`` **不进入** ``route_accuracy`` 分母 -> 该指标
  ``NOT_MEASURED``；
* ``rule_route_agreement`` 仍会算，但它明确标注为「规则分类器 vs 候选标签」，
  不是对外可陈述的准确率。

这不是工具的缺陷，是纪律：拿自己生成的标签验证自己，得到的数字没有意义。

用法（人工操作，不可脚本化）::

    # 1) 导出待确认清单（按业务类别分组，便于分配评审）
    python3 scripts/approve_agent_eval_annotations.py --list --tag 成分与肤质

    # 2) 确认一批。--confirm 传 case_id，--reviewer 传确认人标识。
    #    只有显式列出的 case_id 会被标记，**不会**出现"全选"这种误操作入口。
    python3 scripts/approve_agent_eval_annotations.py \\
        --confirm case_0001 case_0002 --reviewer alice

    # 3) 撤销（发现标错时）
    python3 scripts/approve_agent_eval_annotations.py --revoke case_0001

    # 4) 重新生成数据集（确认结果落到 agent_cases.jsonl）
    python3 scripts/generate_agent_eval_cases.py --apply-approvals approvals.json

确认记录单独保存在 ``tests/eval/agent_annotation_approvals.json``，
**不**直接改写数据集 —— 这样「人工确认」这件事本身是可审计、可回滚、
可被第三方复核的，而不是藏在 111 行 JSONL 的一个布尔字段里。
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evaluation.agent_eval.cases import load_dataset  # noqa: E402

APPROVALS_PATH = REPO_ROOT / "tests" / "eval" / "agent_annotation_approvals.json"


def _load_approvals() -> dict:
    if not APPROVALS_PATH.exists():
        return {"schema_version": "agent-eval-approvals/v1", "approvals": {}}
    return json.loads(APPROVALS_PATH.read_text(encoding="utf-8"))


def _save_approvals(data: dict) -> None:
    APPROVALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPROVALS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _list(args) -> int:
    dataset = load_dataset(args.dataset)
    approvals = _load_approvals()["approvals"]
    cases = dataset.cases
    if args.tag:
        wanted = set(args.tag)
        cases = [c for c in cases if wanted & set(c.tags)]
    if not cases:
        print("no matching cases", file=sys.stderr)
        return 1

    print(f"{len(cases)} case(s) 待/已确认\n")
    for case in cases:
        record = approvals.get(case.case_id)
        status = (
            f"HUMAN-CONFIRMED by {record['confirmed_by']} @ {record['reviewed_at']}"
            if record
            else "llm_candidate (待确认)"
        )
        print(f"  {case.case_id}")
        print(f"      query    : {case.input}")
        print(
            f"      expected : route={case.expected_route} "
            f"state={case.expected_terminal_state} risk={case.expected_risk}"
        )
        print(f"      status   : {status}")
        print()
    confirmed = sum(1 for c in cases if c.case_id in approvals)
    print(f"确认进度：{confirmed}/{len(cases)}")
    return 0


def _mutate(args, ids: list[str], grant: bool) -> int:
    dataset = load_dataset(args.dataset)
    known = {c.case_id for c in dataset.cases}
    unknown = [c for c in ids if c not in known]
    if unknown:
        print(f"❌ 未知 case_id：{unknown}", file=sys.stderr)
        return 2
    if not ids:
        print("❌ 必须显式给出 --confirm/--revoke 的 case_id（不提供全选入口）", file=sys.stderr)
        return 2

    data = _load_approvals()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for case_id in ids:
        if grant:
            if not args.reviewer:
                print("❌ --confirm 必须同时给出 --reviewer（确认人不可为空）", file=sys.stderr)
                return 2
            data["approvals"][case_id] = {
                "confirmed_by": args.reviewer,
                "reviewed_at": now,
                "method": "human_reviewed",
                "expected_route": dataset.by_id(case_id).expected_route,
            }
        else:
            data["approvals"].pop(case_id, None)
    _save_approvals(data)
    verb = "确认" if grant else "撤销"
    print(f"✅ 已{verb} {len(ids)} 条：{ids}")
    print(f"   记录写入 {APPROVALS_PATH.relative_to(REPO_ROOT)}")
    print(
        "   下一步：python3 scripts/generate_agent_eval_cases.py --apply-approvals "
        f"{APPROVALS_PATH.relative_to(REPO_ROOT)}"
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--list", action="store_true", help="导出待确认清单")
    parser.add_argument("--tag", action="append", default=[], help="按标签过滤")
    parser.add_argument("--confirm", nargs="*", default=None, metavar="CASE_ID")
    parser.add_argument("--revoke", nargs="*", default=None, metavar="CASE_ID")
    parser.add_argument("--reviewer", default=None, help="确认人标识（--confirm 必填）")
    args = parser.parse_args(argv)

    if args.list:
        return _list(args)
    if args.confirm is not None:
        return _mutate(args, args.confirm, grant=True)
    if args.revoke is not None:
        return _mutate(args, args.revoke, grant=False)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
