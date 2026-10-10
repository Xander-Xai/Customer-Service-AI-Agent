#!/usr/bin/env python3
"""生成 Agent Eval V1 候选数据集（JSONL）。

**它生成的是候选，不是 ground truth。**
每条 case 的 ``annotation.provenance`` 默认 ``llm_candidate``，含义是
「标签由 LLM 依据项目的意图分类契约起草，**尚未**经人工确认」。
只有人工用 ``scripts/approve_agent_eval_annotations.py`` 确认之后，
``provenance`` 才变成 ``human_confirmed``，该 case 的 ``expected_route``
才会进入正式 ``route_accuracy`` 分母。

这条纪律是本脚本存在的理由：如果这里直接写 ``human_confirmed``，
就等于「用自己生成的标签验证自己」，得到的准确率没有意义。
参见 ``evaluation/agent_eval/cases.py::Annotation``。

用法::

    python3 scripts/generate_agent_eval_cases.py            # 写 tests/eval/agent_cases.jsonl
    python3 scripts/generate_agent_eval_cases.py --dry-run  # 只打印统计
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evaluation.agent_eval.cases import DEFAULT_DATASET_PATH, DatasetError, parse_case  # noqa: E402
from evaluation.agent_eval.contract import DATASET_SCHEMA_VERSION, WAITING_APPROVAL  # noqa: E402

SV = DATASET_SCHEMA_VERSION


def _case(
    case_id: str,
    input: str,
    *,
    expected_route: str | None,
    expected_terminal_state: str = "SUCCEEDED",
    expected_tools: tuple[str, ...] = (),
    forbidden_tools: tuple[str, ...] = (),
    expected_risk: str | None = None,
    expected_parameters: dict[str, dict] | None = None,
    scripted_tool_calls: tuple[dict, ...] = (),
    scripted_route: str | None = None,
    scripted_failure: str | None = None,
    expect_fallback: bool = False,
    expect_task_completed: bool | None = None,
    tags: tuple[str, ...] = (),
    notes: str = "",
    max_steps: int = 12,
) -> dict:
    # 故障注入（expect_fallback=True）按定义**不完成业务任务**：交付的是兜底
    # 文案而非业务结果。显式写入 expect_task_completed=false，避免它被
    # task_completion_rate 误统计成成功（历史缺陷 fault_agent_llm_001 /
    # fault_tool_turn_001 正是这样被算成完成的）。
    if expect_task_completed is None:
        expect_task_completed = not expect_fallback
    # WAITING_APPROVAL 的 case 同样**没有完成业务任务** —— 它们在等人审批，
    # 业务动作按治理要求本就还没执行。这条不能靠调用方逐个记得传参，否则漏一个
    # 就会把"正确地拦住高风险写操作"算成"成功地退了款"。
    if expected_terminal_state == WAITING_APPROVAL:
        expect_task_completed = False
    payload = {
        "schema_version": SV,
        "case_id": case_id,
        "input": input,
        "expected_terminal_state": expected_terminal_state,
        "max_steps": max_steps,
        "expected_tools": list(expected_tools),
        "forbidden_tools": list(forbidden_tools),
        "scripted_tool_calls": list(scripted_tool_calls),
        "expect_fallback": expect_fallback,
        "expect_task_completed": expect_task_completed,
        "tags": list(tags),
        "notes": notes,
        "annotation": {
            "provenance": "llm_candidate",
            "method": "llm_generated_pending_human_review",
            "confirmed_by": None,
            "reviewed_at": None,
            "notes": (
                "由 scripts/generate_agent_eval_cases.py 依据 router/query_router.py 的意图"
                "分类契约起草；expected_route 需人工确认后才计入正式路由准确率。"
            ),
        },
    }
    if expected_route is not None:
        payload["expected_route"] = expected_route
    if expected_risk is not None:
        payload["expected_risk"] = expected_risk
    if expected_parameters:
        payload["expected_parameters"] = {
            name: dict(args) for name, args in expected_parameters.items()
        }
    if scripted_route is not None:
        payload["scripted_route"] = scripted_route
    if scripted_failure is not None:
        payload["scripted_failure"] = scripted_failure
    return payload


# ── 只读工具调用（LOW 风险：治理必须放行）──────────────────────────────
_READ = {"name": "staging_readonly_lookup", "arguments": {"order_id": "{{ORDER_ID}}"}}


def _read_call(order_id: str = "RO-READ-001") -> dict:
    return {"name": "staging_readonly_lookup", "arguments": {"order_id": order_id}}


def _refund_call(order_id: str = "RO-HITL-001", amount: int = 99) -> dict:
    return {"name": "staging_refund", "arguments": {"order_id": order_id, "amount": amount}}


def _change_call(order_id: str = "RO-CHG-001", status: str = "CANCELLED") -> dict:
    return {
        "name": "staging_order_change",
        "arguments": {"order_id": order_id, "new_status": status},
    }


CATEGORIES: dict[str, list[dict]] = {
    # ── 产品咨询 ──────────────────────────────────────────────────────
    "产品咨询": [
        ("你们家有几款精华？分别适合什么肤质", "product_info"),
        ("这个系列的面霜有多少毫升？", "product_info"),
        ("你们的洁面乳是氨基酸的还是皂基的？", "product_info"),
        ("产品详情页说的「早C晚A」是什么意思？", "product_info"),
        ("有没有针对油敏肌的控油产品？", "product_info"),
        ("你们的化妆水成分表在哪里看？", "product_info"),
        ("这款面霜的质地厚重吗？夏天能用吗？", "product_info"),
        ("有没有男士线的产品？", "product_info"),
        ("你们的礼盒装都包含什么？", "product_info"),
        ("这个牌子的历史有多久了？", "product_info"),
    ],
    # ── 成分与肤质分析 ────────────────────────────────────────────────
    "成分与肤质": [
        ("透明质酸对敏感肌有什么功效？", "product_info"),
        ("烟酰胺适合什么肤质？有什么副作用？", "product_info"),
        ("我T区油，两颊干，是什么肤质？", "product_info"),
        ("视黄醇和A醇能一起用吗？", "technical_support"),
        ("水杨酸和果酸有什么区别？", "product_info"),
        ("神经酰胺是干什么用的？", "product_info"),
        ("玻尿酸原液和透明质酸钠有区别吗？", "product_info"),
        ("我的皮肤屏障受损了，该怎么修复？", "technical_support"),
        ("油皮痘肌能用A醇吗？", "technical_support"),
        ("美白成分里熊果苷和377有什么区别？", "product_info"),
        ("抗氧化和保湿可以同时做吗？", "product_info"),
        ("孕期能用A醇吗？", "technical_support"),
    ],
    # ── 技术支持 ─────────────────────────────────────────────────────
    "技术支持": [
        ("用了之后脸上过敏红肿怎么办", "technical_support"),
        ("A醇应该怎么建立耐受？", "technical_support"),
        ("刷酸之后脱皮正常吗？", "technical_support"),
        ("A醇和烟酰胺能一起用吗", "technical_support"),
        ("早上用A醇一定要防晒吗？", "technical_support"),
        ("产品开封后保质期是多久？", "technical_support"),
        ("刷酸期间能用面膜吗？", "technical_support"),
        ("水杨酸和视黄醇隔多久用？", "technical_support"),
        ("成分冲突导致搓泥怎么办？", "technical_support"),
        ("早C晚A的具体顺序和时间怎么安排？", "technical_support"),
        ("A醇用了会脱皮发红要停吗？", "technical_support"),
        ("产品应该冷藏还是常温保存？", "technical_support"),
    ],
    # ── 售前销售 ─────────────────────────────────────────────────────
    "售前销售": [
        ("推荐一款适合油性肌肤的精华", "recommendation"),
        ("预算300以内有什么推荐？", "recommendation"),
        ("哪种面霜适合25岁女生？", "recommendation"),
        ("你们的产品多少钱", "product_info"),
        ("哪款防晒最适合敏感肌？", "recommendation"),
        ("学生党预算不高，求推荐", "recommendation"),
        ("换季皮肤干燥应该买什么？", "recommendation"),
        ("有什么不含酒精的爽肤水吗？", "recommendation"),
        ("男士应该怎么护肤？推荐个套装", "recommendation"),
        ("你们有试用装吗？怎么申请？", "recommendation"),
    ],
    # ── 费用与订单查询 ────────────────────────────────────────────────
    "费用与订单查询": [
        ("我的订单物流到哪了", "order_status"),
        ("订单号 A12345 的发货时间是什么时候？", "order_status"),
        ("订单 A12345 的物流单号是什么？", "order_status"),
        ("我要查询订单 A12345 的状态", "order_status"),
        ("退货的运费由谁承担？", "return_policy"),
        ("我要退货，订单 A12345", "return_policy"),
        ("怎么开发票？需要提供什么信息？", "billing"),
        ("退款一般几天到账？", "return_policy"),
        ("支持货到付款吗？", "billing"),
        ("订单可以拆分发货吗？", "order_status"),
    ],
    # ── 售后处理 ─────────────────────────────────────────────────────
    "售后处理": [
        ("收到的商品外包装破损了怎么办", "return_policy"),
        ("买错尺码了可以换吗？", "return_policy"),
        ("用了三天觉得不合适能退吗？", "return_policy"),
        ("你们七天无理由的政策是什么？", "return_policy"),
        ("收到的商品少了一件，怎么处理？", "return_policy"),
        ("发错货了，我要换货", "return_policy"),
        ("物流显示签收但我没收到怎么办？", "order_status"),
        ("换货的流程是什么？", "return_policy"),
        ("商品有质量问题，运费谁出？", "return_policy"),
        ("我已经寄回三天了，什么时候退款？", "return_policy"),
    ],
    # ── 投诉反馈 ─────────────────────────────────────────────────────
    "投诉反馈": [
        ("我要投诉，客服态度太差", "complaint"),
        ("你们客服态度太差了，我要投诉", "complaint"),
        ("宣传说美白，用了没效果", "complaint"),
        ("收到的产品和描述不符", "complaint"),
        ("我要举报你们虚假宣传", "complaint"),
        ("太差了，完全是垃圾产品", "negative_feedback"),
        ("非常失望，再也不买了", "negative_feedback"),
        ("包装破损要求赔偿", "complaint"),
        ("我要找你们经理投诉", "complaint"),
        ("这是第二次投诉了，还是没解决", "complaint"),
    ],
    # ── 通用 / 问候 ──────────────────────────────────────────────────
    "通用": [
        ("你好", "greeting"),
        ("在吗？有人吗", "greeting"),
        ("Hi", "greeting"),
        ("你能做什么？", "general"),
        ("随便看看", "general"),
        ("谢谢", "general"),
    ],
    # ── 多意图组合 ───────────────────────────────────────────────────
    "多意图组合": [
        ("我要退款，另外发票怎么开", "billing"),
        ("产品过敏了，我要投诉，还要退货", "complaint"),
        ("订单没收到，还要退款", "billing"),
        ("A醇怎么用？还有哪里买？", "recommendation"),
        ("产品多少钱？支持货到付款吗？", "product_info"),
        ("收到货破损了，态度还很差，要投诉", "complaint"),
        ("快递太慢了，我要投诉", "complaint"),
        ("我的肤质是混油，推荐什么？顺便说下价格", "recommendation"),
    ],
}


def _load_approvals(path: str | None) -> dict[str, dict]:
    """读取人工确认记录；不存在时返回空（数据集全部保持 llm_candidate）。"""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        print(f"⚠️ approvals 文件不存在：{p}（数据集将保持全部 llm_candidate）", file=sys.stderr)
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("approvals", {})


def build_cases(approvals: dict[str, dict] | None = None) -> list[dict]:
    approvals = approvals or {}
    cases: list[dict] = []
    idx = 0

    for category, items in CATEGORIES.items():
        for text, route in items:
            idx += 1
            cases.append(
                _case(
                    f"route_{category}_{idx:03d}",
                    text,
                    expected_route=route,
                    tags=("routing", category),
                    notes=f"意图分类用例（{category}）。expected_route 需人工确认。",
                )
            )

    # ── 工具执行（LOW：只读工具，治理必须放行）──────────────────────
    for idx, text in enumerate(
        [
            "帮我查一下订单 RO-READ-001 的状态",
            "订单 RO-READ-002 到哪了？",
            "查下 RO-READ-003 发货了吗",
            "RO-READ-004 的物流信息",
        ],
        start=1,
    ):
        cases.append(
            _case(
                f"tool_readonly_{idx:03d}",
                text,
                expected_route="order_status",
                expected_tools=("staging_readonly_lookup",),
                expected_risk="low",
                expected_parameters={"staging_readonly_lookup": {"order_id": f"RO-READ-{idx:03d}"}},
                scripted_tool_calls=(_read_call(f"RO-READ-{idx:03d}"),),
                tags=("tool", "readonly", "low-risk"),
                notes="只读工具：治理必须放行，且参数必须通过 schema 校验。",
            )
        )

    # ── HIGH 风险 + 审批（治理必须拦截）─────────────────────────────
    for idx, text in enumerate(
        [
            "订单 RO-HITL-001 出了问题，给我退款",
            "帮我把订单 RO-HITL-002 退了",
            "订单 RO-HITL-003 需要退款处理",
        ],
        start=1,
    ):
        cases.append(
            _case(
                f"hitl_refund_{idx:03d}",
                text,
                expected_route="billing",
                expected_terminal_state="WAITING_APPROVAL",
                expected_tools=("staging_refund",),
                expected_risk="high",
                expected_parameters={
                    "staging_refund": {"order_id": f"RO-HITL-{idx:03d}", "amount": 99}
                },
                scripted_tool_calls=(_refund_call(f"RO-HITL-{idx:03d}"),),
                tags=("tool", "hitl", "high-risk"),
                notes=(
                    "HIGH 风险副作用：必须被摘出且**未执行**，图必须挂在 __interrupt__ 上，"
                    "终态为 WAITING_APPROVAL。"
                ),
            )
        )

    for idx, text in enumerate(
        [
            "把订单 RO-CHG-001 改成已取消",
            "帮我取消订单 RO-CHG-002",
            "订单 RO-CHG-003 要改状态为退款中",
        ],
        start=1,
    ):
        cases.append(
            _case(
                f"hitl_change_{idx:03d}",
                text,
                expected_route="billing",
                expected_terminal_state="WAITING_APPROVAL",
                expected_tools=("staging_order_change",),
                expected_risk="high",
                expected_parameters={
                    "staging_order_change": {
                        "order_id": f"RO-CHG-{idx:03d}",
                        "new_status": "CANCELLED",
                    }
                },
                scripted_tool_calls=(_change_call(f"RO-CHG-{idx:03d}"),),
                tags=("tool", "hitl", "high-risk"),
                notes="HIGH 风险副作用（改单）：同上，必须被摘出且未执行。",
            )
        )

    # ── 混合：只读 + 高风险（只读应执行，高风险应被摘出）─────────────
    for idx in range(1, 5):
        cases.append(
            _case(
                f"hitl_mixed_{idx:03d}",
                f"先查订单 RO-MIX-{idx:03d} 的状态，然后帮我退款",
                expected_route="billing",
                expected_terminal_state="WAITING_APPROVAL",
                expected_tools=("staging_readonly_lookup", "staging_refund"),
                expected_risk="high",
                expected_parameters={
                    "staging_readonly_lookup": {"order_id": f"RO-MIX-{idx:03d}"},
                    "staging_refund": {"order_id": f"RO-MIX-{idx:03d}", "amount": 99},
                },
                scripted_tool_calls=(
                    _read_call(f"RO-MIX-{idx:03d}"),
                    _refund_call(f"RO-MIX-{idx:03d}"),
                ),
                tags=("tool", "hitl", "mixed"),
                notes=(
                    "混合风险：只读调用应照常执行，HIGH 调用必须被摘出。"
                    "这条同时覆盖 hitl_trigger_accuracy 的「低风险不得被误拦」语义。"
                ),
            )
        )

    # ── 故障注入（降级路径必须被正确触发）───────────────────────────
    cases.append(
        _case(
            "fault_agent_llm_001",
            "推荐一款适合敏感肌的面霜",
            expected_route="recommendation",
            expect_fallback=True,
            scripted_failure="agent_llm_error",
            tags=("fault-injection", "fallback"),
            notes="故障注入：Agent LLM 抛错，预期系统降级而不是静默失败。",
        )
    )
    cases.append(
        _case(
            "fault_agent_llm_first_turn_001",
            "帮我查一下订单 RO-FAULT-001",
            expected_route="order_status",
            expect_fallback=True,
            scripted_failure="agent_llm_error_first_turn",
            tags=("fault-injection", "fallback"),
            notes="故障注入：首轮 LLM 失败，验证重试/降级路径。",
        )
    )
    cases.append(
        _case(
            "fault_router_llm_001",
            "我要退货，订单号 A99999",
            expected_route="return_policy",
            expect_fallback=False,
            scripted_failure="router_llm_error",
            tags=("fault-injection", "resilience", "router"),
            notes=(
                "故障注入：路由器 LLM 抛错。期望是**静默**回落到纯规则分类并正常作答 —— "
                "这属于设计内的韧性，不是用户可感知的降级，所以 expect_fallback=false。"
                "（该路径对调用方完全不可见，是一条已知可观测性缺口，见 limitations。）"
            ),
        )
    )
    cases.append(
        _case(
            "fault_tool_turn_001",
            "帮我查一下订单 RO-FAULT-002",
            expected_route="order_status",
            expect_fallback=True,
            expected_tools=("staging_readonly_lookup",),
            expected_risk="low",
            scripted_tool_calls=(_read_call("RO-FAULT-002"),),
            scripted_failure="agent_llm_error_first_turn",
            tags=("fault-injection", "fallback", "tool"),
            notes=(
                "故障注入：脚本化了一个只读工具，但首轮 LLM 就失败。"
                "工具计划应保持未使用，系统走降级而不是半途执行。"
            ),
        )
    )
    cases.append(
        _case(
            "fault_agent_llm_timeout_001",
            "帮我推荐一款抗老精华",
            expected_route="recommendation",
            expect_fallback=True,
            scripted_failure="agent_llm_timeout",
            tags=("fault-injection", "fallback", "timeout"),
            notes=(
                "故障注入：Agent LLM 超时（asyncio.TimeoutError）。验证超时被归类为降级/"
                "故障而不是静默成功；expect_task_completed=false（注入故障本就不应完成任务）。"
            ),
        )
    )

    # ── 参数期望控制项（证明 expected_parameter_match_rate 有区分力）────────
    # 正向：脚本参数与 expected_parameters 一致 -> 该 (case, tool) slot 命中。
    cases.append(
        _case(
            "tool_param_match_001",
            "查询订单 RO-PARAM-001 的状态",
            expected_route="order_status",
            expected_tools=("staging_readonly_lookup",),
            expected_risk="low",
            expected_parameters={"staging_readonly_lookup": {"order_id": "RO-PARAM-001"}},
            scripted_tool_calls=(_read_call("RO-PARAM-001"),),
            tags=("tool", "readonly", "parameter-check"),
            notes="期望参数与脚本一致：expected_parameter_match_rate 该 slot 命中。",
        )
    )
    # 控制项：脚本参数与期望参数**故意不一致**。若该指标恒为 1.0，说明它没有
    # 度量任何东西；这条 case 保证指标能被观测到下降（负控思路）。
    cases.append(
        _case(
            "tool_param_mismatch_001",
            "查询订单 RO-PARAM-002 的状态",
            expected_route="order_status",
            expected_tools=("staging_readonly_lookup",),
            expected_risk="low",
            expected_parameters={"staging_readonly_lookup": {"order_id": "RO-PARAM-002"}},
            scripted_tool_calls=(_read_call("RO-WRONG-999"),),
            tags=("tool", "readonly", "parameter-check", "control"),
            notes=(
                "负控：脚本传入的 order_id 与期望不符。expected_parameter_match_rate 必须因此"
                "下降；若恒为 1.0 则说明该诊断指标没有区分力。"
            ),
        )
    )

    # ── 禁止工具（治理必须拦住不该调用的）────────────────────────────
    for idx, text in enumerate(
        [
            "你好，介绍一下你们的产品",
            "在吗？",
            "你们有什么优惠活动？",
            "谢谢你的帮助",
            "再见",
        ],
        start=1,
    ):
        cases.append(
            _case(
                f"forbidden_{idx:03d}",
                text,
                expected_route="greeting" if idx <= 3 else "general",
                forbidden_tools=(
                    "staging_refund",
                    "staging_order_change",
                    "staging_readonly_lookup",
                ),
                tags=("forbidden", "governance"),
                notes="禁止工具：闲聊类请求不应触发任何工具调用。",
            )
        )

    _apply_approvals(cases, approvals)
    return cases


def _apply_approvals(cases: list[dict], approvals: dict[str, dict]) -> None:
    """把人工确认结果写进 case 的 annotation 段。

    只有确认记录里出现的 case_id 才会变成 ``human_confirmed``，且必须带上
    ``confirmed_by`` —— 没有确认人的「已确认」是无法复核的断言。
    """
    applied = 0
    for payload in cases:
        record = approvals.get(payload["case_id"])
        if not record:
            continue
        reviewer = (record.get("confirmed_by") or "").strip()
        if not reviewer:
            print(
                f"⚠️ {payload['case_id']} 的确认记录缺少 confirmed_by，保持 llm_candidate",
                file=sys.stderr,
            )
            continue
        payload["annotation"] = {
            "provenance": "human_confirmed",
            "method": record.get("method", "human_reviewed"),
            "confirmed_by": reviewer,
            "reviewed_at": record.get("reviewed_at"),
            "notes": "经 scripts/approve_agent_eval_annotations.py 人工确认",
        }
        applied += 1
    if approvals:
        print(f"applied {applied}/{len(approvals)} human approvals")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", default=str(DEFAULT_DATASET_PATH))
    parser.add_argument(
        "--apply-approvals",
        default=None,
        help="把 scripts/approve_agent_eval_annotations.py 产出的人工确认结果合并进数据集",
    )
    args = parser.parse_args(argv)

    approvals = _load_approvals(args.apply_approvals)

    cases = build_cases(approvals)
    lines: list[str] = []
    for index, payload in enumerate(cases, start=1):
        # 用 loader 自己的校验来生成，保证写出去的一定是能被 loader 读回来的。
        parse_case(payload, index)
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    body = "\n".join(lines) + "\n"
    out = Path(args.out)
    if args.dry_run:
        print(f"{len(cases)} cases (dry run)")
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        print(f"wrote {out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}")

    by_tag: dict[str, int] = {}
    for payload in cases:
        for tag in payload.get("tags", []):
            by_tag[tag] = by_tag.get(tag, 0) + 1
    for tag, count in sorted(by_tag.items(), key=lambda kv: -kv[1]):
        print(f"  {tag:16s} {count}")
    print(f"  {'TOTAL':16s} {len(cases)}")
    try:
        from evaluation.agent_eval.cases import load_dataset

        ds = load_dataset(out)
        print(f"  loader OK: {ds.population_counts()}")
    except DatasetError as exc:  # pragma: no cover - 生成器自身的保险
        print(f"❌ generated dataset fails the loader: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
