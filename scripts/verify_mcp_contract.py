"""MCP 端到端契约取证脚本：跑真实链路并落 evidence artifact。

与 ``scripts/verify_distributed_runtime.py`` 同一套证据约定：

- ``schema_version`` 带版本号，便于后续演进时区分；
- ``tested_code_sha`` 记录**被测代码**的 SHA，``artifact_commit_sha`` 为 null
  （artifact 在生成之后才提交，提交它会改变 SHA —— 与 runtime evidence 同此约定）；
- ``overall_status`` 只有 PASS / FAIL，没有「部分通过」这种含糊状态。

**它验证的是什么**：本仓 MCP 适配器与**本地 deterministic fake MCP server** 之间的
端到端契约（`registry → adapter → server → result → policy/telemetry`）。
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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCHEMA_VERSION = "mcp-contract-evidence/v1"

#: 契约必须覆盖的工具行为面。每一条都对应 e2e 测试里的一个断言面，缺任何一条
#: 都说明取证不完整 —— 与其让 artifact 声称"全覆盖"却漏测，不如显式失败。
REQUIRED_SCENARIOS = (
    "safe_read",
    "high_risk_side_effect",
    "timeout",
    "oversized",
    "failing",
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _scenario_nodeids() -> dict[str, list[str]]:
    """从 e2e 测试文件里抽出每个行为面的 nodeid（只解析，不执行）。

    这样 artifact 里的 "covered" 是**从真实测试文件里读出来的**，不是手写的
    声明 —— 手写清单和实际测试漂移时无法被发现。
    """
    src = (ROOT / "tests" / "integration" / "test_mcp_contract_e2e.py").read_text(encoding="utf-8")
    mapping = {
        "safe_read": "TestSafeReadContract",
        "high_risk_side_effect": "TestHighRiskSideEffectPolicy",
        "timeout": "TestTimeoutContract",
        "oversized": "TestOversizedContract",
        "failing": "TestFailingToolContract",
    }
    lines = src.splitlines()
    out: dict[str, list[str]] = {}
    for scenario, klass in mapping.items():
        try:
            start = next(i for i, ln in enumerate(lines) if ln.startswith(f"class {klass}"))
        except StopIteration:
            out[scenario] = []
            continue
        nodeids: list[str] = []
        for ln in lines[start + 1 :]:
            if ln.startswith("class ") or ln.startswith("@"):
                break
            stripped = ln.strip()
            if stripped.startswith(("async def test_", "def test_")):
                name = stripped.split("def ", 1)[1].split("(")[0]
                nodeids.append(f"tests/integration/test_mcp_contract_e2e.py::{klass}::{name}")
        out[scenario] = nodeids
    return out


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

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/integration/test_mcp_contract_e2e.py",
        "-v",
        "--tb=short",
        "-rs",
        "-p",
        "no:cacheprovider",
        "--no-cov",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    report = proc.stdout + proc.stderr

    # 收集被跳过的测试 —— 「没跑」必须显式出现在 artifact 里，不能只报 passed。
    skipped = sorted({ln.split("::", 1)[1] for ln in report.splitlines() if " SKIPPED " in ln})

    scenarios = _scenario_nodeids()
    uncovered = [s for s in REQUIRED_SCENARIOS if not scenarios.get(s)]

    overall = "PASS"
    if proc.returncode != 0:
        overall = "FAIL"
    elif skipped:
        # 端到端契约"部分跳过"不是通过：skip 意味着这条契约没被验证。
        overall = "FAIL"
    elif uncovered:
        overall = "FAIL"

    artifact = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tested_code_sha": _git("rev-parse", "HEAD"),
        "artifact_commit_sha": None,
        "overall_status": overall,
        "command": " ".join(cmd),
        "exit_code": proc.returncode,
        "pytest_summary": next(
            (ln.strip() for ln in report.splitlines() if " passed" in ln or " failed" in ln),
            None,
        ),
        "skipped_tests": skipped,
        "scenarios": scenarios,
        "required_scenarios": list(REQUIRED_SCENARIOS),
        "uncovered_scenarios": uncovered,
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
    if skipped:
        print(f"SKIPPED tests (契约未被验证): {skipped}")
    if uncovered:
        print(f"UNCOVERED scenarios: {uncovered}")
    if overall != "PASS":
        print(report[-4000:])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
