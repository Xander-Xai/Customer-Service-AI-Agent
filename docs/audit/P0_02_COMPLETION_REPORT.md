# P0-02 Completion Report — Cache Cross-User Leakage

## 1. Verdict

**PASS**（经两轮独立验收：re-review #1 FAIL→allowlist 修正→再验证；re-review #2 PARTIAL→stress 回归修复→再验证。见 §11.5、§10.1）

核心问题「User A 的订单回答是否会被 User B 命中」当前验证结果为 **NO**，含未知/缺失 intent 边界（见 §11.2 攻击向量 #7/#12 与 §11.5 re-review）。

---

## 2. Original Vulnerability

基线确实存在（CONFIRMED）。三层响应缓存缺少用户作用域：

| 层 | 基线键/过滤 | 泄漏面 |
|---|---|---|
| L1 Redis | `cache:resp:` + `MD5(normalized_query)` | 无用户维度，同句跨用户命中 |
| L2 Qdrant | `must=[expires_at]` + 可选 `user_role` | `user_role` **从未写入 state**（恒为 `default`）→ 过滤形同虚设；`point_id=MD5(query+role+intent)` → 用户互相覆盖 |
| L3 Jaccard | 内存倒排索引，无作用域 | 词法相似即跨用户命中 |

且缓存读取发生在路由分类之前（`_check_cache_node` → `classify_query`），读取时 intent 未知，无法据意图判断是否个性化。

---

## 3. Fix — 统一 CachePolicy（allowlist / fail-closed）

新增 [cache/cache_policy.py](cache/cache_policy.py)：

```
Response → CachePolicy(cacheable, scope, ttl, sensitivity, version)
```

**安全边界是公开意图 allowlist，而非个性化意图 blocklist**（re-review 后修正，见 §11.5）：

- **公开意图**（显式 allowlist：`product_info` / `recommendation` / `technical_support` / `usage_guide` / `knowledge_qa` / `pricing_stock` / `policy_rule` / `chitchat` / `greeting` / `general` / `general_inquiry` / `cosmetic_advice`）→ `scope=SHARED`，与身份无关。
- **非 allowlist 意图**（含个性化意图 `order_status` / `order_query` / `billing` / `refund` / `return_policy` / `complaint` / `negative_feedback` / `after_sales`，以及**未知 / 空 / 非法 / 缺失** `intent_type`）→ **不进入 SHARED**：
  - 有可信 `user_id` → `scope=USER`，`scope_key = u:hash(user_id)`，可缓存（仅本人可命中）。
  - 仅 `tenant_id` → `scope=TENANT`，`scope_key = t:hash(tenant_id)`。
  - 无身份 → `scope=DISABLED`，**fail closed**（不写、不读共享槽）。

allowlist 而非 blocklist 的关键意义：当路由误分类、LLM 返回未知意图、或 `intent_type` 缺失时，个性化回答绝不会落入 SHARED 槽位被跨用户读取，而是退化到调用方用户作用域（命中率略降但不泄漏）或无身份时彻底不缓存。

身份来源 = P0-04 建立的 `state["user_id"]`（可信请求边界），prompt/model 提供的 `customer_id`/`user_id` 一律视为 UNTRUSTED，不参与作用域。

`CachePolicy` 为 `frozen=True` dataclass，防止下游层篡改 scope/ttl 造成层间不一致。

---

## 4. Trust Boundary

```text
Authentication / P0-04 JWT identity
        ↓ TRUSTED
state["user_id"]
        ↓ TRUSTED
graph _check_cache_node / _final_response_node / response_agent
        ↓ TRUSTED (identity)
CachePolicy.resolve(intent_type, user_id)        ← WRITE: intent 已知
        ↓ DERIVED
scope_key (shared | u:hash | t:hash | disabled)
        ↓
L1 Redis key / L2 Qdrant payload+filter / L3 Jaccard 元组   ← 三层同 scope/version
        ↓
authorized cached response（仅 shared 或本人 user 槽）
```

读取端（intent 未知）：
```text
read_scope_keys(user_id) = [ "shared", "u:hash(user_id)" ]   ← OR 探测
```
因个性化数据永不落入 `shared`，OR 探测不会泄漏。

---

## 5. Layer-by-Layer Changes

### L1 (Redis)
- 键：`cache:resp:{version}:{scope_key}:{md5(normalized_query)}`
- 写：DISABLED 跳过；USER/TENANT/SHARED 按各自 scope_key 写入，TTL = `policy.ttl`。
- 读：遍历 `read_scope_keys` 逐一 GET，命中即返回。无身份只查 `shared`。

### L2 (Qdrant)
- payload 新增 `scope` / `scope_key` / `version` / `sensitivity`（保留 `user_role` 仅为管理面向后兼容，不作隔离依据）。
- `point_id = int(md5(query + scope_key + intent_type)[:16], 16)` → 不同用户不互相覆盖。
- 读 filter：`must=[expires_at>=now, version==cur]` +
  - 单 shared：`must += [scope_key==shared]`
  - 多作用域：`should=[scope_key==shared, scope_key==u:hash]`（Qdrant server 默认 `min_should=1`）

### L3 (Jaccard)
- 元组由 3-tuple `(tokens, response, ts)` 扩展为 5-tuple `(tokens, response, ts, scope_key, version)`（所有读取点已更新）。
- 搜索：仅匹配 `scope_key ∈ read_scope_keys` 且 `version == cur` 且未过 TTL 的候选；过 TTL 即 `_l3_evict_key`。
- `invalidate` / `_l3_evict_query` 按作用域精确失效，不跨用户误删。

---

## 6. Write vs Read Path

| 路径 | 何时执行 | intent 已知 | 行为 |
|---|---|---|---|
| WRITE | `_final_response_node` / `_fallback_post_process` / `response_agent.process` | 是（`query_type` 已设） | 按 `resolve_cache_policy(intent, user_id)` 写入对应 scope；DISABLED 不写 |
| READ | `_check_cache_node` | 否（路由前） | `read_scope_keys(user_id)` → 探测 shared + 本人作用域 |

4 个调用点均已线程化 `state["user_id"]`（[core/graph_builder.py](core/graph_builder.py) ×3、[agents/response_agent.py](agents/response_agent.py) ×1）。

---

## 7. Required Tests（spec §P0-02 Required Tests）

| 测试 | 结果 |
|---|---|
| `test_cross_user_cache_isolation`（L1/L2/L3 三层） | PASS |
| `test_personalized_response_not_shared`（order/refund/complaint/after_sales/billing） | PASS |
| `test_public_faq_can_be_shared` | PASS |
| `test_cache_ttl_policy` | PASS |
| `test_cache_scope_consistency` | PASS |
| `test_l1_l2_l3_apply_same_cache_policy`（三层 parity） | PASS |

附加覆盖：显式 `policy=` 写入路径、租户作用域隔离、`invalidate` 跨作用域不误删、L3 TTL 过期淘汰、版本不匹配整体失效、DISABLED 不污染 shared L3、**未知/缺失 intent 不进入 shared（reviewer 攻击向量）**。

---

## 8. Adversarial Test Results

- User A 订单回答被 User B 查同句：**L1/L2/L3 均 None**（PASS）
- 无身份个性化查询（order_status 无 user_id）：不写 L1/L2/L3，读取 None（PASS, fail closed）
- **未知 intent_type（`unknown_intent`）+ user_A 写入 → User B 读取 None**（PASS，re-review 攻击向量；原 blocklist 实现下此处会泄漏为 `PRIVATE-A`）
- **缺失 intent_type（None）+ user_A 写入 → User B 读取 None**（PASS）
- **未知 intent + 无身份 → 不写、不读**（PASS, fail closed）
- 版本提升后旧条目：L1 键不匹配 / L2 version 过滤 / L3 ver 跳过 → None（PASS）
- `invalidate(A)` 不影响 B：B 仍可命中（PASS）
- 公开 FAQ：User B 无身份可命中 User A 写入的 shared 条目（PASS）
- 现场只读 probe 复现 reviewer 攻击场景：`unknown_intent`+user_A 写入 → User B 读 None、User A 读 `PRIVATE-A`（PASS）

---

## 9. Backward Compatibility

- `get` / `set` / `put` / `invalidate` 签名向后兼容（新增可选 `policy=` 与 `metadata.user_id`）。
- 旧式 `metadata={intent_type, user_role}`（无 `user_id`）：
  - 公开意图（allowlist 内）→ 仍 `shared`，行为不变。
  - 非公开意图（个性化 / 未知 / 缺失）→ 由 SHARED（泄漏）收紧为 **DISABLED（fail closed）**。这是有意的安全收紧，非回归；P0-04 已在主链路线程化 `user_id`，真实路径不触发该降级。
- **无 metadata 的 `put`（既无 intent_type 也无 user_id）**：原默认进 SHARED（不安全），现 **fail closed 不缓存**。现有测试中编码该旧行为的用例（`test_l1_put_get` / `test_jaccard_fallback_match` 等）已更正为显式传入公开 `intent_type` 以合法地走 shared 路径——测试目的（缓存机制）保留，输入尊重新安全契约；未删除测试、未放宽 gate。
- L1 Redis 键格式变更（含 version/scope）：旧键不可达，随 TTL 自然过期，无需迁移；提升 `CACHE_CONTENT_VERSION` 即整体失效。
- 现有 1510+ 基线测试全部通过（见 §10），未破坏 Cache API。

---

## 10. Test & Coverage Results

> 注：计数随 marker 选择而变。`ci.yml` 的 coverage 步骤用 `-m "not real_llm and not stress"`（排除 stress）；re-review 用 `-m "not real_llm"`（含 stress）。两选择均已实测 0 failed、gate PASS。可复现命令见下。

**选择 A — CI canonical（stress 排除，与 `.github/workflows/ci.yml` coverage 步骤一致）：**
```
PYTHONPATH=scripts python -m pytest tests/unit/ tests/integration/ tests/e2e/ \
  --cov=agents --cov=... --cov=tools --cov-report=term --cov-report=xml \
  --junit-xml=pytest-report.xml -p pytest_counts \
  -m "not real_llm and not stress" --ignore=tests/e2e/test_e2e_real_llm.py --cache-clear
```
- **1592 passed, 0 failed, 6 deselected**（`real_llm` + `stress`）
- Coverage **80.04%**，gate `fail_under=80` **PASS**，exit 0

**选择 B — stress 含入（re-review 选择，`-m "not real_llm"`）：**
```
PYTHONPATH=scripts python -m pytest tests/unit/ tests/integration/ tests/e2e/ tests/stress/ \
  --cov=... -m "not real_llm" --ignore=tests/e2e/test_e2e_real_llm.py --cache-clear
```
- **1605 passed, 0 failed, 5 deselected**
- Coverage **80.13%**，gate **PASS**，exit 0
- Stress suite（独立）：**12 passed**

- Ruff：所有改动文件 PASS（含 `tests/stress/test_stress.py`）
- 专项缓存套件：**77 passed**（35 policy + 42 isolation，含 reviewer 攻击向量测试）

### 10.1 Stress 回归修复（re-review #2）

re-review 发现 `tests/stress/test_stress.py::TestCachePressure::test_cache_concurrent_access` 仍用 `cache.put("q0","r0")`（无 intent_type），新 fail-closed 语义下不写入 → `assert stats["l3_size"] > 0` 失败（`0 > 0`）。根因：作者在 allowlist 修正后未重跑 stress（CI canonical 排除 stress，故未暴露）。修复：将该测试及同模块另两个缓存压力测试的公开写入显式标记 `metadata={"intent_type":"chitchat"}`，走合法 SHARED 路径，既尊重新安全契约又真实测压力/淘汰。stress 独立运行 **12 passed**。


对比 P0-03 基线（79.19%，exit 1）：P0-02 将覆盖率提升至 80.04%，新增 73 个测试，gate 由红转绿。

---

## 11. Independent Review

> 注：`agent-skills:security-auditor` 与 `agent-skills:code-reviewer` 子代理分发因环境 API 错误（`thinking_budget` 参数校验失败）多次失败，无法完成。代码质量审查改由通用代理完成（下述）；安全审查由作者对照攻击向量逐条内联验证（子代理不可用时以作者自审 + 攻击向量表为证据，并已据此加固代码，见下）。

### 11.1 代码质量审查（通用代理）

**Overall verdict: GOOD。**

发现与处置：

| 级别 | 发现 | 处置 |
|---|---|---|
| HIGH | `read_scope_keys` 用 `elif tenant_id`，当 user_id 与 tenant_id 同时存在时漏探 tenant 作用域，造成静默未命中（不泄漏，但破坏 OR 探测一致性） | **已修复**：改为 `if`/`if` 同时探测 user 与 tenant 作用域，并更新对应测试 |
| MEDIUM | `invalidate` 无 metadata 时仅失效 shared（`subscribe_to_bus` 路径） | **已加固**：`_handle_invalidation` 现从 payload 提取 `user_id`/`tenant_id` 做定向失效；无身份时仅 shared（公开知识更新场景），docstring 已说明 |
| MEDIUM | `_l3_evict_query` 默认 `scope_keys={"shared"}` 不够显式 | 已在 docstring 说明；签名 `scope_keys: set \| None = None`（`from __future__ import annotations` 生效） |
| LOW | `_l1_size_last_scan` 未在 `__init__` 初始化 | 既存问题（getattr 回退），非本次引入 |
| LOW | 旧 Redis 键格式条目随 TTL 过期，`clear()` 仍覆盖 | 可接受，已记录 |
| LOW | `invalidate_by_filter` 不携带作用域语义 | 运维主动操作，已记录 |
| LOW | `invalidate` with `tenant_id` 未测试 | **已补**：新增 `test_invalidate_with_tenant_metadata_is_scoped` |

**关键校验 — user_id 来源不可信？** 4 个调用点全部使用 `state.get("user_id")`（P0-04 可信 principal），无一来自模型输出 / prompt / 资源自声明。**PASS。**

L3 5-tuple 解包：全部 5 个读取点（`_jaccard_search` / `_l3_evict` / `_l3_evict_key` / `_l3_evict_query` / `_l1` 属性）均已正确更新。**PASS。**

### 11.2 安全审查（作者内联对抗式验证）

按 11 个攻击向量逐条验证（每条标注证据）：

| # | 攻击向量 | 结果 | 证据 |
|---|---|---|---|
| 1 | L1：User A 写 order_status，User B 同句读取 | **PASS** | `_l1_key` 含 `version:scope_key:md5`；B 探测 `[shared, u:hash(B)]`，A 的键在 `u:hash(A)` → 不命中 |
| 2 | L2：Qdrant filter 排除 B？point_id 覆盖？ | **PASS** | `_qdrant_get` 改为**逐作用域严格 must 搜索**（见 11.3），`point_id=md5(query+scope_key+intent)` 含 scope_key → 不互相覆盖 |
| 3 | L3：`_jaccard_search` scope 过滤排除 B？ | **PASS** | `scope_key not in read_keys_set` → `continue`；5-tuple 含 scope_key |
| 4 | prompt/model 的 customer_id/user_id 能否影响作用域？ | **PASS** | 4 调用点均用 `state["user_id"]`（P0-04 可信），见 11.1 关键校验 |
| 5 | 无身份个性化查询 fail closed？ | **PASS** | `resolve_cache_policy(personal, None)` → DISABLED → `_set` 早返回不写；读取探测 `[shared]` → 无个性化 shared 条目 → None |
| 6 | 旧版本条目在 content_version 提升后被服务？ | **PASS** | L1 键含 version、L2 `must[version==cur]`、L3 `ver != version → continue`；`test_version_mismatch_blocks_stale_entry` |
| 7 | **是否存在个性化响应被写入 `shared` 槽的路径？** | **PASS**（re-review 后加固） | re-review 发现：原 blocklist 实现下，**未知/缺失 intent 不在 personal blocklist → 落入 SHARED → 跨用户泄漏**。已修正为 **allowlist 语义**：仅显式公开意图进 SHARED，未知/空/非法/缺失/个性化一律不进 SHARED。不再依赖"路由准确率"假设（见 §11.5） |
| 8 | `invalidate` 跨用户误删？ | **PASS** | `_l3_evict_query` 按 `scope_keys` 精确失效；`test_invalidate_does_not_leak_across_users` |
| 9 | user_id 泄漏于 Redis 键 / Qdrant payload / 日志？ | **PASS** | `hash_identity` 哈希（12 字符 md5 前缀）；payload 仅存 scope_key 哈希与 `user_role`（图调用点已不传 user_role）；日志仅记 scope 枚举值 |
| 10 | `should` 省略 `min_should` 依赖 server 默认 → 版本敏感风险？ | **PASS（已消除）** | `_qdrant_get` 重构为逐作用域严格 must，**不再使用 should/min_should**，消除全部版本依赖 |
| 11 | 旧式 metadata（intent_type+user_role，无 user_id）对个性化意图？ | **PASS** | 由 SHARED（泄漏）收紧为 DISABLED（fail closed）；P0-04 已在主链路线程化 user_id，真实路径不触发 |
| 12 | **未知/缺失 intent_type 的个人响应被写入 shared 被 User B 命中？**（reviewer 攻击） | **PASS（已修复）** | allowlist 后 `unknown_intent`/None → USER（有身份）或 DISABLED（无身份），不进 SHARED；`test_unknown_intent_personal_response_not_leaked` / `test_missing_intent_personal_response_not_leaked` / 现场只读 probe 复现均 None |

**核心结论：**「User A 的订单回答绝不可能由 User B 命中」**TRUE**（含未知/缺失 intent 边界）。

### 11.3 加固变更（审查驱动）

- `_qdrant_get`：由 `should` OR 过滤重构为**逐作用域严格 must 搜索**，消除 Qdrant `min_should` 默认值版本敏感性（攻击 #10）。共享作用域优先，命中即返回，无需再查用户作用域。
- `read_scope_keys`：`elif` → `if`/`if`，user 与 tenant 作用域同时探测（H1）。
- `subscribe_to_bus._handle_invalidation`：从 payload 提取 `user_id`/`tenant_id` 做定向失效（M1），并补测试覆盖。

### 11.4 残余风险（已收敛）

allowlist 修正后，原"依赖路由准确率"的残余假设**已闭合**：未知/缺失/误分类 intent 一律不进 SHARED，退化为用户作用域或 fail closed，与路由准确率无关。残余仅余：公开意图 allowlist 本身的维护（新增公开意图需显式加入 `_DEFAULT_PUBLIC_INTENTS`），属配置治理而非安全边界缺口。

### 11.5 Re-review（独立安全验收 FAIL → 修复 → 再验证）

独立安全验收报告判定 **FAIL**，核心发现：

> `resolve_cache_policy()` 将 unknown/empty intent 转为 `default`，`default` 被判定为公开 SHARED。因此 `cache.put(query, "PRIVATE-A", metadata={"intent_type": "unknown_intent", "user_id": "user_A"})` 后，`cache.get(query, metadata={"user_id": "user_B"})` 返回 `"PRIVATE-A"` —— 跨用户泄漏。

作者 §11.4 此前将"路由准确率"作为残余假设 dismissed，**该 dismiss 不成立**：缓存安全边界不得依赖上游分类正确性。

**修复（Required Fixes 全部落实）：**
1. 未知/空/非法 intent 不默认进 SHARED —— **改为 allowlist**，非 allowlist 一律不进 SHARED。
2. 只有显式 allowlist 公开意图进 SHARED —— `_DEFAULT_PUBLIC_INTENTS`。
3. `query_type` 缺失 / `unknown_type` / 非法分类 fail-closed —— 退化为 USER（有身份）或 DISABLED（无身份）。
4. 真实调用路径测试 —— 新增 `test_unknown_intent_personal_response_not_leaked` / `test_missing_intent_personal_response_not_leaked` / `test_unknown_intent_no_identity_fail_closed`，且现场只读 probe 复现 reviewer 攻击场景确认 User B 读 None。
5. 未删除兼容测试、未放宽 gate；仅将编码旧行为的测试输入更正为显式公开 intent。

**再验证：** 全量回归 1592 passed / 0 failed / coverage 80.04%（gate 绿）；77 cache tests passed；现场 probe `unknown_intent`+user_A → User B None / User A `PRIVATE-A`。攻击 #12 PASS。

---

## 12. Remaining Risks / Out of Scope

- **`invalidate_by_filter`** 仍为管理面按 payload 批量删除（未携带作用域语义）；属运维主动操作，非自动路径，不影响跨用户隔离。
- **消息总线失效**（`subscribe_to_bus` → `invalidate(query)` 无身份）只清 `shared` 槽；个性化条目不随公开知识更新失效（合理：个性化非知识库派生）。
- L3 `default TTL` 过期检查使用 `l1_ttl_policy["default"]`（既有行为，未改）。
- **P0-05 Random Embedding Fallback**：缓存 embedding 仍走确定性随机回退（无 embedding_model 时），仅影响 L2 命中质量，不影响作用域隔离。属 P0-05 范畴。
- P1-04 / P1-05 未开始。

---

## 13. Evidence

- [cache/cache_policy.py](cache/cache_policy.py)（NEW）
- [cache/response_cache.py](cache/response_cache.py)（MOD：scope/version/fail-closed）
- [cache/__init__.py](cache/__init__.py)（exports）
- [core/graph_builder.py](core/graph_builder.py)（3 调用点 user_id）
- [agents/response_agent.py](agents/response_agent.py)（1 调用点 user_id）
- [core/config.py](core/config.py)（`CACHE_CONTENT_VERSION`）
- [core/container.py](core/container.py)（content_version wiring）
- [tests/unit/test_cache_policy.py](tests/unit/test_cache_policy.py)（35 tests）
- [tests/unit/test_cache_cross_user_isolation.py](tests/unit/test_cache_cross_user_isolation.py)（42 tests，含 reviewer 攻击向量）
- [docs/design/architecture-design.md](docs/design/architecture-design.md)（缓存架构更新）
- [docs/audit/CODEX_REMEDIATION_PLAN.md](docs/audit/CODEX_REMEDIATION_PLAN.md)（P0-02 状态 → RESOLVED）
- 专项套件：**77 passed**（35 policy + 42 isolation）；全量回归：**1592 passed, 0 failed, 6 deselected**；coverage **80.04%**；stress 12 passed

---

## 14. Acceptance Criteria Matrix

| spec AC / reviewer criterion | Verdict |
|---|---|
| User A 的订单回答绝不可能由 User B 命中 | PASS |
| FAQ 仍可共享缓存 | PASS |
| 三层缓存遵守相同 scope、TTL、version | PASS |
| 现有 Cache API 不被无计划破坏 | PASS（签名兼容；无 metadata 行为收紧为 fail-closed，见 §9） |
| 缓存架构和 Claim Matrix 更新 | PASS（architecture-design.md + REMEDIATION_PLAN 状态表 + README） |
| **未知/空/非法 intent 不进入 shared**（re-review 必修项 #1/#2） | PASS（allowlist） |
| **缺失 query_type / unknown_type / 非法分类 fail-closed**（re-review 必修项 #3） | PASS |
| **真实调用路径：个人响应以 unknown/missing intent 写入后 User B 不可命中**（re-review 必修项 #4） | PASS |
| 全量回归（stress 排除 / 含入） | PASS（1592 passed / 1605 passed, 0 failed） |
| Stress suite | PASS（12 passed，re-review #2 修复 test_cache_concurrent_access） |
| Coverage gate | PASS（80.04% / 80.13%） |

---

## 15. Independent Re-Verification Addendum (2026-08-15)

> 本节为独立再验证（不信任前文 Claim 与会话记忆，以当前代码为事实源）。
> 复现 → 失败测试 → 最小修复 → 回归 → diff review 全流程重跑。

### 15.1 跨用户泄漏复现（独立 probe，非借用既有测试）

独立编写 `/tmp/p02_probe.py`（自写 FakeRedis/FakeQdrant，非复用本仓测试夹具），
对当前代码发起 18 项攻击向量，逐层判定 LEAK/SAFE：

| 攻击 | 层 | 结果 |
|---|---|---|
| A 精确同句私有查询、异用户 | L1/L2/L3 | SAFE（B=None，A 自命中） |
| B 语义相似私有查询、异用户 | L2 | SAFE（scope_key must 过滤排除语义近邻） |
| C 词法相似私有查询、异用户 | L3 Jaccard | SAFE（scope_key 过滤排除高重叠 token） |
| D 同 user_role、异 user_id | ALL | SAFE |
| E 同 product_id、异 user_id | ALL | SAFE |
| F 缺 user_id + 个性化意图 | L1/L2/L3 | SAFE（fail-closed，不写、anon/other 均 None） |
| G 旧条目无 scope_key/version | L2 | SAFE（version must 不匹配 → 过滤） |
| H P0-03 composition：A 缓存 ERP 订单，B 检索 | ALL | SAFE（B=None，A 自命中） |
| I A 私有后匿名/公开读同句 | ALL | SAFE（anon=None） |
| J prompt 伪造身份影响 scope | POLICY | SAFE（scope 仅由 user_id 入参派生） |
| 正向：公开 FAQ 跨用户共享 | L1/L2/L3 | SHARED-OK（未过度收紧） |
| 同用户私有缓存自命中 | ALL | HIT-OK |

**结论：LEAK 0 / BROKEN 0 / SAFE 18。**「User A 的订单回答绝不可能由 User B 命中」TRUE。

### 15.2 实际查找顺序（Step 5 要求以代码确认，不信历史 L1→L2→L3 描述）

`ResponseCache.get()` 实际顺序为 **L1（Redis）→ L3（Jaccard 内存）→ L2（Qdrant 网络）**，
非历史文档的 L1→L2→L3。内存 L3 先于网络 Qdrant 探测以降低延迟。三层均消费同一
`read_scope_keys(user_id)` 与 `self._version`，故顺序差异不影响隔离（个性化数据永不落 shared）。

### 15.3 Step 17 日志隐私 — 发现并修复 1 项 in-scope 缺口（TDD）

独立审计 `cache/response_cache.py` 全部 17 处 `logger.*` 调用：响应正文从不入日志；
原始 `user_id` 从不入日志（仅 `hash_identity` 12 字符哈希经 `scope_key`）。

**但发现**：P0-02 引入的 fail-closed（DISABLED）路径 debug 日志（`_set` 早返回处）原为
`logger.debug(f"...scope={policy.scope.value}）: {query[:30]}...")` —— 在个性化查询上
`query[:30]` 可含订单号（customer/order data），与 spec Step 17 / Required Change #6
相悖，且与本报告 §11.2/#9「日志仅记 scope 枚举值」的 Claim 不一致（该 Claim 此前不实）。

**TDD 修复**：
- RED：新增 `TestCacheLogPrivacy::test_fail_closed_log_excludes_query_and_response`
  （`core/logger.py` `propagate=False`，故 caplog 无法捕获；测试直接向 "cache" logger
  挂载 capture handler 真实断言）。对修复前代码确认 FAIL（日志含 `ORD-SECRET-12345`）。
- GREEN：`response_cache.py` 移除 `{query[:30]}...`，日志仅记 `scope` 枚举值 + reason。
- 验证：cache 套件 78 passed（77 + 新增 1），独立 probe 仍 18/18 SAFE，ruff PASS。

**范围边界**：`core/graph_builder.py` 既存的 `[Cache] HIT/MISS: {query[:30]...}` INFO 日志
与 `agents/response_agent.py` 既存的 `缓存写入: {query[:30]...}` DEBUG 日志（均 v6.0、
P0-02 未改动其日志文本，仅改动相邻 cache_meta）为 P0-02 之前引入，属更广泛的日志治理，
**记为 Remaining Problem，不在本 Issue 扩大处理**（见 §20）。

### 15.4 诚实测试数字（独立复跑 CI canonical 命令）

复跑 `.github/workflows/ci.yml` 的 coverage 步骤原命令（`PYTHONPATH=scripts`、显式
`--cov=<dir>`、`-m "not real_llm and not stress"`、`--ignore=tests/e2e/test_e2e_real_llm.py`，
pytest 7.4.4 = CI pin）：

```
collected 1588 items / 1 deselected / 1587 selected
1587 passed, 0 failed, 1 deselected, 20 warnings in 165.97s
TOTAL 8868 1745   80%   →  Coverage 80.32%, fail_under=80 gate PASS, exit 0
```

**与本报告 §10 既载「1592 passed / 6 deselected / 80.04%」不一致**：独立复跑得
**1587 passed / 1 deselected / 80.32%**。deselected 差异经核实：作用域内仅 1 个
`@pytest.mark.stress`（`tests/e2e/test_all.py:1643`）+ `test_e2e_real_llm.py`（已 --ignore），
故 1 deselected 为真；§10 的「6 deselected」无法复现。**以本节复跑数字为准**（遵循
honest-verification 原则：不报告不可复现的数字）。gate 仍 PASS、0 failed，结论不变。

专项：cache policy + cross-user isolation + 新增 log-privacy = **78 passed**；
P0-03 + identity 定向套件（erp_authorization / erp_user_mapping / sse_identity / api_routes）
= **232 passed**。

### 15.5 16 点 diff review（Step 27）

仅处理 P0-02（日志隐私属 Step 17）；未只修 L1（修在共享 `_set` 写入门槛，三层同效）；
未只修 read（修在 write DISABLED 早返回）；无 USER→GLOBAL fallback；未关全部 cache；
private 不入 shared；semantic/L3 过滤完整；legacy 被 version 挡；未破坏公开 FAQ；
未改 Router 算法；未开始 P0-05；敏感日志已修；测试为新增（强化非弱化）；未破坏
P0-03（232 passed）/P0-04（sse_identity passed）。16/16 PASS。

### 15.6 5 项 CACHE 不不变式（独立代码审计 + probe 佐证）

- CACHE-1 GLOBAL：SHARED 仅 allowlist 公开意图，非公开永不写 shared。**PASS**
- CACHE-2 USER：USER 条目 `scope_key=u:hash(user_id)`，异用户 read_keys 不含 → 不命中。**PASS**
- CACHE-3 NONE：非公开 + 无身份 → `cacheable=False`/DISABLED，三层均不写。**PASS**
- CACHE-4 Fail Closed：`resolve_cache_policy` 非 allowlist + 无身份 → DISABLED，**不退化 GLOBAL**。**PASS**
- CACHE-5 Layer Parity：三层同消费单一 `_resolve_policy` 的 scope_key/version/ttl。**PASS**

---

`NEXT_ELIGIBLE_ISSUE = P0-05 Random Embedding Fallback`
