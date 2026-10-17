"""生成 monitoring/alert_rules_test.yml 里 reconciler 相关的 5 个场景。

从 alert_rules.yml 读取 annotation 原文写入期望值，避免两份文案漂移。promtool 要求
exp_annotations 逐字匹配，人工维护两份必然不同步。

幂等：脚本每次从 marker 之后整体重写该段，可重复运行。
"""

import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
RULES = ROOT / "monitoring/alert_rules.yml"
TESTS = ROOT / "monitoring/alert_rules_test.yml"

MARKER = "# ═══════════════════════════════════════════════════════════════════════════"

# promtool 测试数据里的实际失败计数（用于渲染 {{ $value | humanize }}）
FAIL_COUNT = 10


def build() -> str:
    rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    by_name = {r["alert"]: r for g in rules["groups"] for r in g["rules"]}
    stalled = by_name["AgentReconcileStalled"]["annotations"]
    failing = by_name["AgentReconcileScanFailing"]["annotations"]
    stalled_desc = stalled["description"]
    failing_desc = failing["description"].replace("{{ $value | humanize }}", str(FAIL_COUNT))
    rb = stalled["runbook_url"]

    def ann(summary: str, desc: str, runbook: str | None = None) -> str:
        out = f'              summary: "{summary}"\n              description: "{desc}"\n'
        if runbook:
            out += f'              runbook_url: "{runbook}"\n'
        return out

    return f"""
{MARKER}
# 恢复闭环（reconciler）停滞告警 —— 五个必须覆盖的场景
#
# 关键点：**beat 存活与扫描成功是两个正交信号**。beat 的健康检查只看 schedule 文件
# mtime，进程活着就一直更新，所以「beat 活着但扫描一直失败」时它是绿的。这里用
# 「最近一次成功扫描距今多久」而不是失败计数，因此调度停止与扫描失败都能被发现。
#
# 本段的 exp_annotations 由 scripts/sync_alert_rule_test_annotations.py 从
# alert_rules.yml 生成，不要手工改文案。
{MARKER}

  # ── 场景 1：正常扫描 -> 绝不触发 ──────────────────────────────────────────
  # gauge 跟随 wall-clock 递增，因此 time() - gauge 恒为 0。
  - interval: 1m
    input_series:
      - series: 'agent_reconcile_last_success_timestamp_seconds'
        values: '0+60x60'
    alert_rule_test:
      - eval_time: 60m
        alertname: AgentReconcileStalled
        exp_alerts: []

  # ── 场景 2：时间戳停更（beat 不再调度，或扫描一直失败）-> 必须触发 ────────
  - interval: 1m
    input_series:
      - series: 'agent_reconcile_last_success_timestamp_seconds'
        values: '100+0x60'
    alert_rule_test:
      - eval_time: 30m
        alertname: AgentReconcileStalled
        exp_alerts:
          - exp_labels:
              severity: critical
            exp_annotations:
{ann(stalled["summary"], stalled_desc, rb)}
  # ── 场景 3：指标从未出现（beat 刚部署 / 尚未成功扫描过一次）-> 必须触发 ──
  # absent() 分支。没有它，刚上线时会有一段无监控窗口：闭环可能从一开始就是断的。
  - interval: 1m
    input_series:
      - series: 'up{{job="app",instance="a"}}'
        values: '1+0x60'
    alert_rule_test:
      - eval_time: 30m
        alertname: AgentReconcileStalled
        exp_alerts:
          - exp_labels:
              severity: critical
            exp_annotations:
{ann(stalled["summary"], stalled_desc, rb)}
  # ── 场景 4：扫描持续失败（beat 仍调度，但扫描抛错）-> 失败计数告警触发 ────
  - interval: 1m
    input_series:
      - series: 'agent_reconcile_scan_total{{outcome="error"}}'
        values: '0+1x40'
    alert_rule_test:
      - eval_time: 20m
        alertname: AgentReconcileScanFailing
        exp_alerts:
          - exp_labels:
              severity: warning
              outcome: "error"
            exp_annotations:
{ann(failing["summary"], failing_desc)}
  # ── 场景 5：历史失败过一次、之后无新增 -> 失败计数告警不得 latching ──────
  # 若有人把 increase(...[10m]) > 0 改成 > 0，这里会立刻失败：计数器停在 12
  # 不再增长但表达式恒真，告警永久挂着。
  - interval: 1m
    input_series:
      - series: 'agent_reconcile_scan_total{{outcome="error"}}'
        values: '12+0x60'
    alert_rule_test:
      - eval_time: 60m
        alertname: AgentReconcileScanFailing
        exp_alerts: []
"""


def main() -> int:
    text = TESTS.read_text(encoding="utf-8")
    idx = text.find("\n" + MARKER)
    if idx == -1:
        print("marker not found; appending section", file=sys.stderr)
        new = text.rstrip() + "\n" + build()
    else:
        new = text[:idx].rstrip() + "\n" + build()
    TESTS.write_text(new, encoding="utf-8")
    print("regenerated reconciler rule-test section")
    return 0


if __name__ == "__main__":
    sys.exit(main())
