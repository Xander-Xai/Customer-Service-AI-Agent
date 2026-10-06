"""MCP 端到端契约取证脚本：跑真实链路并落 evidence artifact。

与 ``scripts/verify_distributed_runtime.py`` 同一套证据约定：

- ``schema_version`` 带版本号，便于后续演进时区分；
- ``tested_code_sha`` 记录**被测代码**的 SHA（脏工作树追加 ``+dirty``），
  ``artifact_commit_sha`` 为 null（artifact 在生成之后才提交，提交它会改变 SHA ——
  与 runtime evidence 同此约定）；
- ``overall_status`` 只有 PASS / FAIL，没有「部分通过」这种含糊状态。

**为什么不能拿 exit code 当判据（v1 的缺陷）**

pytest 对 ``xfail``（非 strict）与非 strict 的 ``xpass`` 都返回 exit code 0。所以
"这条 required scenario 被 xfail 掉了"和"所有 required scenario 真的通过了"在
exit code 上**不可区分**。v1 只判断 ``returncode == 0`` 且 ``-v`` 文本里没有
``SKIPPED``，于是把一条 xfail 掉的契约照样写成 ``overall_status: PASS``。

换 JUnit XML（``--junit-xml``）也救不了：pytest 8.x 把非 strict 的 ``xpass`` 写成
一个**没有子元素的普通 ``<testcase>``**，与真 pass 一样。``xfail`` 才会被标成
``<skipped type="pytest.xfail">``。也就是说 xpass 在 JUnit XML 里同样会漏。

**v2 的做法：内嵌 pytest 插件 → machine-readable 结果**

插件挂在 ``pytest_runtest_logreport`` / ``pytest_collectreport`` 上，直接读**原始**
``TestReport``（``outcome`` + ``wasxfail`` + ``when``）。这是 xfail / xpass 唯一存在
的地方。插件把每条 nodeid 归类成六种互斥结论::

    passed | failed | skipped | xfailed | xpassed | error

判定**只读**这份 JSON；``-v`` 文本只留给人看（``pytest_summary`` / 失败时回显）。
归不出已知结论的记录一律按 ``error`` 处理（fail-closed），不存在"未知即通过"。

**PASS 的充要条件**（任一不满足即 FAIL，理由逐条落在 ``fail_reasons`` 里）：

1. 插件产出的 machine-readable 结果可解析，且**收集到至少一条**测试；
2. pytest exit code == 0；
3. collection 层没有 error / skip（收集失败是"什么都没验证"）；
4. **全部**被执行的测试结论都是 ``passed`` —— ``failed`` / ``skipped`` /
   ``xfailed`` / ``xpassed`` / ``error`` 任意一条出现即 FAIL（不区分是否 required：
   同一份 evidence run 里存在没跑成的断言，整份 artifact 就不能声称"全覆盖"）；
5. 每个 required scenario 都**至少收集到一条** nodeid（缺场景 = 没测）；
6. 每个 required scenario 的所有 nodeid 结论都是 ``passed``；
7. required scenario 类里**静态声明**的每个 ``def test_*`` 都真的被执行到了
   （挡住"声明了却被 deselect / 漏收集"被读成通过）。

skip 之所以算 FAIL：测试文件顶部用 ``pytest.importorskip("mcp")``，官方 SDK 缺失时
整份契约会**静默 skip** —— 跳过的证据不是证据。xfail / xpass 同理：xpass 意味着这
条契约当前是"预期失败"，即**没有被验证**。因此命令行还显式加
``-o xfail_strict=true``，让 pytest 自身的 exit code 与本脚本的结论一致，避免出现
"artifact 说 FAIL、裸 pytest 说绿"的分裂。

**它验证的是什么**：本仓 MCP 适配器与**本地 deterministic fake MCP server** 之间的
端到端契约（``registry → adapter → server → result → policy/telemetry``）。
client 侧是**官方 mcp SDK** + **真实子进程 + 真实 stdin/stdout 管道**。

**它不验证什么**（artifact 里显式列出，避免被过度解读）：真实第三方 MCP server、
生产网络与鉴权、多副本部署、写操作 MCP 工具。本地 fake server 成立，**不等于**
接入任意第三方 server 也成立。

用法::

    python3 scripts/verify_mcp_contract.py --output artifacts/mcp/<ts>/report.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

SCHEMA_VERSION = "mcp-contract-evidence/v2"

TEST_MODULE = "tests/integration/test_mcp_contract_e2e.py"

#: 插件模块名。必须是合法 Python 标识符（``-p`` 走 importlib.import_module），
#: 且临时目录要挂到 PYTHONPATH 上，pytest 才能 import 到它。
PLUGIN_MODULE = "mcp_contract_evidence_plugin"

#: 环境变量名：插件把 machine-readable 结果写到这里。用环境变量而不是 pytest
#: 自定义 CLI option，是因为本仓库**不允许**给这条命令再挂 conftest/option 改动，
#: 而 env 在 ``-p`` 加载阶段就已经可见。
PLUGIN_OUT_ENV = "MCP_CONTRACT_PYTEST_OUT"

#: 契约必须覆盖的工具行为面 → 测试文件里对应的 class。每一条都对应 e2e 测试里的
#: 一个断言面，缺任何一条都说明取证不完整 —— 与其让 artifact 声称"全覆盖"却漏测，
#: 不如显式失败。
SCENARIO_CLASSES: dict[str, str] = {
    "safe_read": "TestSafeReadContract",
    "high_risk_side_effect": "TestHighRiskSideEffectPolicy",
    "timeout": "TestTimeoutContract",
    "oversized": "TestOversizedContract",
    "failing": "TestFailingToolContract",
}
REQUIRED_SCENARIOS = tuple(SCENARIO_CLASSES)

#: 六种互斥结论。只有 ``passed`` 算验证通过。
OUTCOMES = ("passed", "failed", "skipped", "xfailed", "xpassed", "error")
NON_PASS_OUTCOMES = tuple(o for o in OUTCOMES if o != "passed")

#: PASS 的充要条件，同时写进 artifact 的 ``result_source.pass_requires``。
PASS_REQUIRES = (
    "machine-readable 结果可解析且 collected > 0",
    "pytest exit code == 0",
    "collection 层无 error / skip",
    "所有被执行的测试结论都是 passed（failed/skipped/xfailed/xpassed/error 任意一条即 FAIL）",
    "每个 required scenario 至少收集到一条 nodeid",
    "每个 required scenario 的所有 nodeid 结论都是 passed",
    "required scenario 类里静态声明的每个 def test_* 都真的被执行到",
)

#: 判定用的 pytest 插件源码。内嵌成字符串而不是新增一个文件，是为了让这份取证
#: 逻辑只活在一个文件里（判定与取证必须同源，分开就会出现"改了一处忘了另一处"）。
_OUTCOME_PLUGIN = r'''
"""Session-wide pytest outcome recorder.

由 verify_mcp_contract.py 用 ``-p mcp_contract_evidence_plugin`` 加载，
``MCP_CONTRACT_PYTEST_OUT`` 指向要写出的 JSON。

为什么必须是插件而不是解析 ``-v`` 文本或看 exit code：pytest 对 xfailed 与非 strict
xpassed 都返回 0；JUnit XML 把非 strict xpass 渲染成空的 ``<testcase>``。两者都把
"验证过"和"没验证过"压成同一个值。只有原始 ``TestReport`` 上的 ``wasxfail`` +
``outcome`` + ``when`` 携带这个区别。
"""

from __future__ import annotations

import json
import os

_OUT_PATH = os.environ.get("MCP_CONTRACT_PYTEST_OUT") or None

# 一条测试会产出 setup/call/teardown 多个 report，取最严重的结论：teardown 报错
# 绝不能被降级成 "passed"。
_PRECEDENCE = {
    "passed": 0,
    "skipped": 10,
    "xfailed": 20,
    "xpassed": 30,
    "failed": 40,
    "error": 50,
}
_TESTS = {}
_COLLECTION = []

#: strict xpass（``xfail_strict=true``）是 pytest 里唯一**不带 wasxfail** 的 xfail 形态：
#: ``_pytest/skipping.py::pytest_runtest_makereport`` 只把 longrepr 换成这个前缀，
#: 顺手把 outcome 改成 failed。pytest 自己的 ``pytest_report_teststatus`` 同样认不出
#: 它（没有 wasxfail 可查），所以只能嗅这个标记 —— 少这一步，strict xpass 就会被
#: 记成"断言失败"，证据里就看不出"这条契约其实已经不需要 xfail 了"。
_STRICT_XPASS_MARKER = "[XPASS(strict)]"


def _describe(report):
    """把 longrepr 压成一行可读文本（skip 是三元组，其余是 reprcrash / 字符串）。"""
    longrepr = getattr(report, "longrepr", None)
    if longrepr is None:
        return None
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        text = str(longrepr[2])
    else:
        crash = getattr(longrepr, "reprcrash", None)
        text = str(getattr(crash, "message", None) or longrepr)
    return text.strip()[:500] or None


def _strict_xpass_reason(report):
    longrepr = getattr(report, "longrepr", None)
    text = None if longrepr is None else str(longrepr).strip()
    if text and text.startswith(_STRICT_XPASS_MARKER):
        return text[len(_STRICT_XPASS_MARKER):].strip() or "strict xpass"
    return None


def _classify(report):
    """把一条 TestReport 映射到 (六种互斥结论之一, 原因或 None)。"""
    was_xfail = getattr(report, "wasxfail", None)
    strict_xpass = _strict_xpass_reason(report)
    if report.failed:
        # xpass 语义是"意外通过"：strict 形态没有 wasxfail，只能认 longrepr 标记。
        if strict_xpass is not None or was_xfail is not None:
            return "xpassed", strict_xpass or str(was_xfail)
        # 只有 call 阶段的 failure 是断言失败；setup/teardown 失败说明这条测试
        # 根本没跑到完成态，归 error。
        if report.when == "call":
            return "failed", _describe(report)
        return "error", _describe(report)
    if report.passed:
        # 非 strict xpass：outcome 被提升成 passed，但 wasxfail 还在。
        if was_xfail is not None:
            return "xpassed", str(was_xfail)
        return "passed", None
    if report.skipped:
        if was_xfail is not None:
            return "xfailed", str(was_xfail)
        return "skipped", _describe(report)
    return "error", _describe(report)


def pytest_runtest_logreport(report):
    if _OUT_PATH is None:
        return
    verdict, reason = _classify(report)
    previous = _TESTS.get(report.nodeid)
    if previous is None or _PRECEDENCE[verdict] >= _PRECEDENCE[previous["outcome"]]:
        _TESTS[report.nodeid] = {
            "outcome": verdict,
            "phase": report.when,
            "reason": str(reason)[:500] if reason else None,
        }


def pytest_collectreport(report):
    """收集阶段失败/跳过：一条测试都没产出，"0 passed" 必须是 FAIL 而不是空过。"""
    if _OUT_PATH is None:
        return
    if report.failed:
        _COLLECTION.append(
            {
                "nodeid": report.nodeid,
                "outcome": "error",
                "reason": str(report.longrepr)[:1000],
            }
        )
    elif report.skipped:
        _COLLECTION.append(
            {
                "nodeid": report.nodeid,
                "outcome": "skipped",
                "reason": _describe(report) or "collection skipped",
            }
        )


def _pytest_version():
    try:
        import pytest

        return str(pytest.__version__)
    except Exception:
        return "unknown"


def pytest_sessionfinish(session, exitstatus):
    if _OUT_PATH is None:
        return
    counts = dict.fromkeys(_PRECEDENCE, 0)
    for record in _TESTS.values():
        counts[record["outcome"]] += 1
    payload = {
        "pytest_exitstatus": int(exitstatus),
        "pytest_version": _pytest_version(),
        "python_version": ".".join(str(p) for p in __import__("sys").version_info[:3]),
        "collected": len(_TESTS),
        "counts": counts,
        "tests": _TESTS,
        "collection": _COLLECTION,
    }
    # 先写 .partial 再 os.replace：写一半被 kill 也不会留下一个"看起来合法"的
    # 半截 JSON（主脚本读到坏 JSON 会 fail-closed，但没必要先制造那种歧义）。
    tmp = _OUT_PATH + ".partial"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, _OUT_PATH)
'''


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _provenance() -> dict[str, Any]:
    """被测代码的来源信息。

    脏树必须显式标注：否则 artifact 里的裸 SHA 会被读成"这段被测代码可复现"，
    而它其实不是。这里把 **untracked 也算进 dirty**（比 runtime evidence 更严），
    因为本脚本的"覆盖了哪些契约"是从测试文件读出来的 —— 未纳入版本控制时，那份
    覆盖清单根本不可复现。
    """
    sha = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    return {
        "tested_code_sha": f"{sha}+dirty" if dirty else sha,
        "artifact_commit_sha": None,
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "working_tree_dirty": dirty,
    }


def _class_of(nodeid: str) -> str | None:
    parts = nodeid.split("::")
    if len(parts) >= 3 and parts[-2].startswith("Test"):
        return parts[-2]
    return None


def _method_of(nodeid: str) -> str:
    return nodeid.split("::")[-1].split("[", 1)[0]


def _declared_methods(src: str, klass: str) -> list[str] | None:
    """静态抽出 ``class {klass}`` 体内所有 ``def test_*``（含被装饰器隔开的方法）。

    **只做交叉校验**，不作为覆盖判据：覆盖与否以 pytest **实际收集**到的 nodeid 为准
    （那才是真实发生了什么）。这份静态清单的唯一用途是发现"类里声明了却根本没执行"
    的测试 —— 那种情况在只看收集结果时会被漏掉。
    """
    lines = src.splitlines()
    head = re.compile(rf"^class\s+{re.escape(klass)}\b")
    body = re.compile(r"^\s+(?:async\s+)?def\s+(test_\w+)")
    try:
        start = next(i for i, line in enumerate(lines) if head.match(line))
    except StopIteration:
        return None
    names: list[str] = []
    for line in lines[start + 1 :]:
        if re.match(r"^class\s+\w", line):
            break
        found = body.match(line)
        if found:
            names.append(found.group(1))
    return names


def _scenario_report(
    src: str | None, tests: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """按 required scenario 汇总：实际收集了什么、每条结论是什么、漏了什么。"""
    report: dict[str, dict[str, Any]] = {}
    for scenario, klass in SCENARIO_CLASSES.items():
        nodeids = sorted(n for n in tests if _class_of(n) == klass)
        declared = _declared_methods(src, klass) if src is not None else None
        outcomes = {n: tests[n]["outcome"] for n in nodeids}
        not_passed = {n: o for n, o in outcomes.items() if o != "passed"}
        collected_methods = {_method_of(n) for n in nodeids}
        report[scenario] = {
            "class": klass,
            "class_declared_in_source": declared is not None,
            "declared_tests": declared or [],
            "collected_nodeids": nodeids,
            "outcomes": outcomes,
            "covered": bool(nodeids),
            "all_passed": bool(nodeids) and not not_passed,
            "not_passed": not_passed,
            # 声明了却没执行 → FAIL。反过来（执行了但没声明）只记录不失败：
            # 多覆盖永远比少覆盖安全，判据仍然是收集结果。
            "declared_but_not_run": sorted(set(declared or []) - collected_methods),
        }
    return report


def _normalise_outcome(raw: Any) -> str:
    return raw if raw in OUTCOMES else "error"


def _load_outcome_report(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("tests"), dict):
        return None
    payload["tests"] = {
        nodeid: {**record, "outcome": _normalise_outcome(record.get("outcome"))}
        for nodeid, record in payload["tests"].items()
        if isinstance(record, dict)
    }
    return payload


def _fail(reasons: list[dict[str, str]], code: str, detail: str) -> None:
    reasons.append({"code": code, "detail": detail})


def _pytest_text_summary(report: str) -> str | None:
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.startswith(("=", "-")) and (
            " passed" in stripped or " failed" in stripped or " error" in stripped
        ):
            return stripped
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP 端到端契约取证")
    parser.add_argument(
        "--output",
        default=None,
        help="evidence artifact 输出路径（默认 artifacts/mcp/<UTC 时间戳>/report.json）",
    )
    args = parser.parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = Path(args.output) if args.output else ROOT / "artifacts" / "mcp" / ts / "report.json"
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    outcomes_path = out_path.with_name("pytest-outcomes.json")

    reasons: list[dict[str, str]] = []

    src: str | None = None
    try:
        src = (ROOT / TEST_MODULE).read_text(encoding="utf-8")
    except OSError as exc:
        _fail(reasons, "test_module_missing", f"{TEST_MODULE} 不可读：{exc}")

    with tempfile.TemporaryDirectory(prefix="mcp-contract-evidence-") as tmp:
        plugin_dir = Path(tmp)
        (plugin_dir / f"{PLUGIN_MODULE}.py").write_text(_OUTCOME_PLUGIN, encoding="utf-8")
        env = dict(os.environ)
        env[PLUGIN_OUT_ENV] = str(outcomes_path)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(plugin_dir), *filter(None, [env.get("PYTHONPATH")])]
        )
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            TEST_MODULE,
            "-v",
            "--tb=short",
            "-rs",
            "-p",
            "no:cacheprovider",
            "-p",
            PLUGIN_MODULE,
            "--no-cov",
            # 见模块 docstring：让 pytest 自身的 exit code 与本脚本结论一致，
            # 避免 artifact 说 FAIL 而裸 pytest 说绿。
            "-o",
            "xfail_strict=true",
        ]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    text_report = proc.stdout + proc.stderr

    outcome_report = _load_outcome_report(outcomes_path)
    if outcome_report is None:
        _fail(
            reasons,
            "pytest_outcome_report_unavailable",
            f"未取到 machine-readable 结果（{_rel(outcomes_path)}）：插件未加载、"
            f"pytest 进程被强杀，或输出无法解析 → 无法区分 passed / xfail / xpass",
        )
        tests: dict[str, dict[str, Any]] = {}
    else:
        tests = outcome_report["tests"]

    if proc.returncode != 0:
        _fail(reasons, "pytest_exit_nonzero", f"pytest 退出码 {proc.returncode}")

    if outcome_report is not None:
        if not tests:
            _fail(reasons, "no_tests_collected", "收集到 0 条测试：没有任何契约被验证")
        for item in outcome_report.get("collection", []):
            outcome = _normalise_outcome(item.get("outcome"))
            if outcome != "passed":
                _fail(
                    reasons,
                    f"collection_{outcome}",
                    f"{item.get('nodeid')}：{item.get('reason')}",
                )
        for non_pass in NON_PASS_OUTCOMES:
            offenders = sorted(n for n, rec in tests.items() if rec["outcome"] == non_pass)
            if offenders:
                listed = ", ".join(offenders[:20])
                more = f"（共 {len(offenders)} 条）" if len(offenders) > 20 else ""
                _fail(
                    reasons,
                    f"non_pass_outcome_{non_pass}",
                    f"{len(offenders)} 条 {non_pass}{more}: {listed}",
                )

    scenarios = _scenario_report(src, tests)
    for scenario, detail in scenarios.items():
        if not detail["covered"]:
            _fail(
                reasons,
                "scenario_uncovered",
                f"{scenario}（{detail['class']}）未收集到任何测试：这条契约没被验证",
            )
        elif not detail["all_passed"]:
            broken = sorted(
                f"{nodeid}={verdict}" for nodeid, verdict in detail["not_passed"].items()
            )
            _fail(reasons, "scenario_not_all_passed", f"{scenario} 未全部 passed：{broken}")
        if detail["declared_but_not_run"]:
            _fail(
                reasons,
                "scenario_declared_test_not_run",
                f"{scenario} 声明但未执行：{detail['declared_but_not_run']}",
            )

    overall = "FAIL" if reasons else "PASS"

    counts = dict.fromkeys(OUTCOMES, 0)
    for record in tests.values():
        counts[record["outcome"]] += 1

    artifact = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **_provenance(),
        "overall_status": overall,
        "fail_reasons": reasons,
        "command": " ".join(cmd),
        "exit_code": proc.returncode,
        "result_source": {
            "mechanism": f"内嵌 pytest 插件 {PLUGIN_MODULE}.py"
            "（pytest_runtest_logreport / pytest_collectreport）→ machine-readable JSON",
            "raw_report": _rel(outcomes_path),
            "verdicts": list(OUTCOMES),
            "why_not_exit_code": "pytest 对 xfailed 与非 strict xpassed 都返回 exit code 0；"
            "JUnit XML 也把非 strict xpass 渲染成空的 <testcase>。"
            "两者都把『契约被验证』和『契约是预期失败』压成同一个值。"
            "唯一携带该区别的是原始 TestReport 的 wasxfail + outcome + when。",
            "why_exit_code_still_checked": "exit code 单独不足以判通过，但仍是必要条件："
            "collection 崩溃 / INTERNALERROR 等不一定产出逐条 report 的失败模式由它兜底。",
            "pass_requires": list(PASS_REQUIRES),
        },
        "environment": {
            "python": (outcome_report or {}).get("python_version", sys.version.split()[0]),
            "pytest": (outcome_report or {}).get("pytest_version", "unknown"),
            "platform": platform.platform(),
        },
        "totals": {"collected": len(tests), **counts},
        "outcomes": {
            name: sorted(n for n, rec in tests.items() if rec["outcome"] == name)
            for name in OUTCOMES
        },
        "required_scenarios": list(REQUIRED_SCENARIOS),
        "scenario_classes": SCENARIO_CLASSES,
        "scenarios": scenarios,
        "uncovered_scenarios": [s for s, d in scenarios.items() if not d["covered"]],
        "non_passed_scenarios": [
            s for s, d in scenarios.items() if d["covered"] and not d["all_passed"]
        ],
        "pytest_summary": _pytest_text_summary(text_report),
        "transport": {
            "client": "官方 mcp SDK (mcp.client.stdio + ClientSession)",
            "server": "tests/integration/fake_mcp_server.py (本地确定性对端)",
            "mechanism": "真实子进程 + 真实 stdin/stdout 管道",
            "external_public_mcp_service_used": False,
        },
        "not_verified": [
            "真实第三方 MCP server（公共 MCP 生态）",
            "生产网络连通性与鉴权",
            "多副本 / 分布式部署下的 MCP 行为",
            "写操作 MCP 工具（无幂等 ledger + 人工审批接线，read-only-first）",
            "响应侧结果大小上限（当前不设上限，属已知缺口）",
        ],
    }
    out_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"evidence: {out_path}")
    print(f"overall_status: {overall}")
    print(f"totals: {json.dumps(artifact['totals'], ensure_ascii=False)}")
    for reason in reasons:
        print(f"FAIL [{reason['code']}]: {reason['detail']}")
    if overall != "PASS":
        print(text_report[-4000:])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
