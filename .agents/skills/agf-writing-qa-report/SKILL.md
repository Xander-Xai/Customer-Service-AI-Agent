---
name: agf-writing-qa-report
description: Use when qa-engineer (or miniapp-qa-engineer) is about to publish an E2E or UAT report. Provides the report skeleton, evidence-quality bar, verdict criteria, and hand-off rules. SIT is now dev-owned and lives in progress/<role>.md (see agf-running-sit-tests skill) — this skill does NOT cover SIT reports.
---

# Writing a QA Report (E2E / UAT)

Use this skill when:

- E2E execution is complete and a report needs publishing
- UAT business sign-off needs to be captured
- A bug-fix E2E re-verification needs recording (always **appended** as `## Re-run [N] — [date]` to the existing `[feature]-e2e-[YYYY-MM-DD].md`; never a new file — see "File path & naming" below)

**Pair with**:
- `agf-running-sit-tests` skill — SIT 由 dev 自跑，本 skill 不覆盖 SIT 报告（SIT 证据已在 `progress/<role>.md` 的 `**SIT 证据**` 段，归档随 `docs/qa/<feature>-process-log.md` 走）
- This skill — covers the **E2E / UAT artifact** (report file) format

## File path & naming

`docs/qa/[feature-kebab-case]-[stage]-[YYYY-MM-DD].md` — Stage ∈ `{e2e, uat}`. Examples:
- `docs/qa/oauth-login-e2e-2026-05-13.md`
- `docs/qa/oauth-login-uat-2026-05-15.md`

**One report per stage per feature.** Re-runs after defect fix → append a new `## Re-run [N] — [date]` section to the same file, do not create a new file.

## Required sections (in order)

```markdown
# QA Report — [Feature] — [E2E|UAT]

- **Date**: YYYY-MM-DD
- **Stage**: E2E / UAT
- **Tester**: qa-engineer ([model name]) / 业务方姓名（UAT）
- **Branch**: [branch + commit hash]
- **Environment**: local docker-compose / staging / pre-prod
- **PRD**: docs/prd/[feature]-[date].md
- **Code review (含 SIT Audit)**: docs/reviews/[feature]-[date].md

## Summary

- Total AC: N
- Passed: M
- Failed: K
- Blocked: J
- **Verdict**: ✅ Promote to next stage / ❌ Block / ⚠️ Conditional promote

## Pre-conditions Checked

- [ ] 单元测试 + lint + typecheck 全绿
- [ ] code-reviewer 报告已存在且 verdict ≠ Block（含 SIT Audit = ✅ / ⚠️）
- [ ] PRD AC 可访问
- [ ] 环境就绪（DB 起来 / 迁移已 apply / 服务已启动）

任何一条没勾 → 不该开始测；先 SendMessage product-lead 解决先决条件。

## AC Results

### AC-1 (P0): [verbatim AC text from PRD]

- **Priority**: P0 / P1 / P2（来自 PRD §4 Priority；P0 必须跑 2 次，P1/P2 跑 1 次即可）
- **Setup**: [起始状态]
- **Action**: [触发步骤]
- **Expected**: [复制 PRD AC 原文]
- **Actual (run 1)**:
  ```
  HTTP/1.1 200 OK
  Content-Type: application/json
  {"id": 42, "status": "created"}
  ```
- **Actual (run 2)** [P0 必填；P1/P2 可空]:
  ```
  HTTP/1.1 200 OK
  ...
  ```
- **Reliability**: `pass^1 = 1/1` 或 `pass^2 = 2/2`（P0）— 两次不一致 = `⚠️ Flaky`，按 fail 处理
- **Verdict**: ✅ Pass / ❌ Fail / ⚠️ Blocked / ⚠️ Flaky

(每个 AC 都要单独一节，**禁止合并写 'all passed'**。**为什么 P0 要跑 2 次**：业界实证（τ-bench）pass@1 高 ≠ pass^k 高，单次过的 P0 case 偶发问题会逃逸到生产。)

## Defects Found

| ID | Severity | Title | Repro steps | Suspected file |
|---|---|---|---|---|
| DEF-1 | High | ... | 1. ... 2. ... | backend/app/foo |

Severity 标准:
- **Critical**: 阻断核心流程，无 workaround
- **High**: 阻断核心流程但有 workaround；或非核心流程的数据/安全问题
- **Medium**: 边缘场景失败，不阻断核心流程
- **Low**: 体验/文案/兼容性

## Cross-stage Notes

- E2E → UAT: 给业务方的操作手册 / 数据准备说明 / 已知 P2 defect 列表

## Cost (this QA session)

- Tokens consumed: [from `/usage`]
- Estimated cost: [CNY]
- 同 feature 累计（E2E + UAT 总和）：[CNY]

## Hand-off

✅ Promote → SendMessage product-lead 进下一阶段
❌ Block → SendMessage product-lead 列 critical defect，重新派回 dev
⚠️ Conditional → 列 P2 defect 单独建 issue，allow 进下一阶段
```

## Verdict 决策树（不能凭感觉）

```
任一 P0 AC = Fail              → ❌ Block
所有 P0 + P1 AC = Pass         → ✅ Promote
P0 全 Pass，P1 部分 Fail        → ⚠️ Conditional（P1 失败必须建跟踪 issue）
有 P0 = Blocked（环境问题）     → ⚠️ Block + 升级 product-lead
```

## Evidence 质量条

每条 Pass 的 Actual 段**必须**有可验证产物之一：

- HTTP 调用：`curl -i` 完整响应头 + body（敏感字段可遮）
- DB 状态：`SELECT` 前后对比
- UI：截图（命名规则：`evidence/AC-N-[step].png`）
- 日志：相关行（带时间戳）
- 文件落盘：`ls -la` + 内容 head

**禁止**只写"Passed, looks correct"——这种 Pass 不可信，等同没测。

## 反模式

（"合并写 all passed" / "无 evidence 的 Pass" / "Defect 不写 Repro" 已由下文「完成前的验证」checklist 反向守门，此处不重列。）

- ❌ 跑 E2E 时同时改代码（移动靶）— 必须 freeze 分支再跑
- ❌ 用生产 API key 跑 E2E — 必须用专用测试 key + 每日花费上限
- ❌ 用本 skill 写 SIT 报告 — SIT 已 dev 自跑，证据落 `progress/<role>.md`，不再有独立 SIT 报告

## 完成前的验证

- [ ] 每条 AC 都有 Setup / Action / Expected / Actual / Verdict 五段？
- [ ] 每个 Pass 都有可验证 evidence？
- [ ] Defects 表每行都有 Repro steps + Suspected file？
- [ ] Cost 一节填了实际数字（不是 TBD）？
- [ ] Verdict 由决策树推出（不是凭感觉）？
- [ ] Hand-off SendMessage 已发出？

任一不行 → 不要 publish，回去补。

## Hand-off 触发

报告落盘后立即（**不等用户问**）：

1. SendMessage product-lead，附 verdict + report path + 1 句话总结
2. 如 verdict = Block：列 top-3 critical defect 在消息正文
3. 如 verdict = ⚠️ Conditional：把 P2 defect 通过 `TaskCreate` 单独开 follow-up task（由 product-lead 派发）
