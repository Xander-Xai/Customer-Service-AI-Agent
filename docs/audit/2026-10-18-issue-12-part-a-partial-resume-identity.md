# Issue #12 Part A 残差 — 同参写操作「部分恢复」身份专项（2026-10-18）

> **最终安全复核补充（覆盖下文早期自动恢复结论）**：当历史 ledger 同时包含
> SUCCEEDED 的 A₁ 和 PENDING 的 A₂，而恢复模型只提出一个无显式
> `logical_operation_id` 的同参数 A 时，单靠内容和预占槽位**无法判断**
> 该 A 是 A₁ 重放还是 A₂ 首次执行。早期“PENDING 优先、自动执行 A₂”
> 在 A₁ 真重放场景会误执行第二次退款，因此**不再允许**。
> 当前实现对这种缩短的混合状态恢复 **fail-closed**，不执行任何新的副作用；
> 对完整 `[A₁,A₂]` 重放按持久化 occurrence 顺序恢复，已完成项去重，
> 未完成项执行一次；明确持久化的业务 `logical_operation_id` 才能支持
> 单条 A₂ 的无歧义恢复。同时，异步 Run 的解析器不可用时禁止回退内存计数。
> 后文关于“只提出 A₂ 即自动执行”的测试与指标属修复前的历史快照，
> 不得引用为当前行为。外部 Exactly-Once 仍 NOT PROVIDED。

> **对齐报告（HISTORICAL AUDIT SNAPSHOT）**。绑定本修复当时的代码与测试；不硬编码 HEAD，
> 用 `git rev-parse HEAD` 获取。证据状态词：`REPRODUCED` / `VERIFIED_LOCAL` /
> `NOT_VERIFIED` / `NOT PROVIDED`。

## 1. 问题（本次专项的唯一目标）

PR #15（内容寻址身份）修复了「同类写序列**重排**导致重复执行」。本次只回答一个更窄的
问题：**同参数、不同业务逻辑的两次写操作，在「部分恢复」下是否会被错误地合并/吞掉。**

```text
A  = {"order_id": "ORD-001", "amount": 100}
A₁ = refund(A)   # 第一笔合法退款
A₂ = refund(A)   # 第二笔合法退款，参数相同、业务意图不同

attempt 1:  A₁ -> occurrence=0 -> K0 -> SUCCEEDED
            A₂ -> occurrence=1 -> K1   （被提出，但 worker 在 A₂ 落账前死亡）
recovery:   LLM 只重新提出 A₂
            A₂ -> occurrence=0 -> K0   （节点内计数器从 0 重启）
                               ^^^^^^ 撞上 A₁ 已成功的键 -> 静默复用 A₁ 的结果
```

## 2. 结论

**`REPRODUCED`（生产可达路径中证明发生误去重）。**

修复前，`agents/base_agent.py::_process_with_tools` 里写操作的逻辑身份来自**节点局部**
的 `_content_occurrence` 计数器（按「同一节点内相同内容的第 n 次」分槽）。该计数器是
方法内局部变量：worker 崩溃后整节点重放时从 0 重启。恢复时若只再次提出第二笔同参写，
它被重新编号到 occurrence 0、命中第一笔已 SUCCEEDED 的 `operation_key`，`claim()` 返回
`CLAIM_SUCCEEDED`，`execute_idempotent_operation` 直接返回第一笔的结果——**第二笔合法写
从未执行，也没有任何拒绝或人工确认信号**。

判别性证据（本仓实测，`tests/unit/test_side_effect_partial_resume_identity.py::test_case_b_*`）：

```text
attempt 1 (A₁) 副作用工具走幂等 ledger op=v2:d4ec240c87dfff16d1f18ce51786aacaf5da406adeb76dbc04ff226747235d3b
recovery  (A₂) 副作用工具走幂等 ledger op=v2:d4ec240c87dfff16d1f18ce51786aacaf5da406adeb76dbc04ff226747235d3b
E       assert 1 == 2                 # handler 只执行了一次：A₂ 被静默吞掉
```

两次尝试命中**完全相同的 operation_key**（`v2:d4ec240c…`）；`external` 展示的是
「下游无幂等接口」的形状，因此这一次静默去重等价于**一笔退款未发出**。

### 2.1 为何这是可达的（不是纯 builder 单测）

| # | 追踪点 | 事实 |
|---|---|---|
| 1 | LangGraph checkpoint 粒度 | 节点边界提交；工具循环（`_process_with_tools`）整体在**一个协作模式节点**内（`core/graph_builder.py`） |
| 2 | 能否从工具循环中途恢复 | 不能。`_process_with_tools` 是普通方法，无 `interrupt`；节点抛异常则整节点不提交 |
| 3 | 崩溃后重放粒度 | 整节点重放（`runtime/bootstrap.py::invoke_graph_with_resume` 用 `ainvoke(None)` 从 checkpoint 的 `next` 续跑，协作节点整体重跑） |
| 4 | 各部分持久化时机 | checkpoint=节点边界；side-effect ledger=每次工具调用 `claim()`；**occurrence 计数器不持久化** |
| 5 | LLM 重放是否可能给出不同序列 | 可能（LLM 非确定）；本专项不得假定序列一致 |
| 6 | `_logical_ordinals` 是否从 0 重启 | 是（方法局部变量） |
| 7 | 是否有持久化 `logical_operation_id` 可还原 A₂ 身份 | **没有**：Agent 工具循环始终不传 `logical_operation_id`（仅 HITL 走 `approval:{id}`） |
| 8 | 其它围栏是否令其不可达 | 无：HITL 闸门只拦 HIGH 风险；execution fence 只防并发接管；均不覆盖本场景 |

「整节点重放」不等于「LLM 必然生成与首次相同的调用序列」——本次复现正是利用这一点：
第 1 次提出 `[A, A]`，恢复只提出 `[A]`，于是同内容出现次数收缩，身份重编号碰撞。

## 3. 修复：把身份锚定到**持久化**的 occurrence

> 设计文档：[runtime/side_effects.py](../../runtime/side_effects.py)、新模块
> [runtime/operation_identity.py](../../runtime/operation_identity.py)。

内容寻址身份本身没错，错在 occurrence「只由本次尝试的 LLM 输出计数」这一**不可恢复**的
来源。修复把 occurrence 的来源改为**持久化 ledger 快照 + 执行前预占**：

1. `SideEffectStore.reserve_operation(...)`：在执行任何写之前，把本轮全部同内容
   occurrence 的身份以 **PENDING、无 owner** 的行持久化（唯一约束 + `ON CONFLICT DO
   NOTHING`）。崩溃也不会丢「本尝试提出过哪些写」。
2. `ContentOccurrenceResolver`（新模块）在每次尝试开始时**快照**已有 occurrence，按执行
   顺序把每个写提案解析为：
   - **未执行的预占**（PENDING/FAILED，例如被中断的第 2 笔）→ 复用并**执行**；
   - 否则**已成功**且未被消费的 occurrence → 复用（真重放去重）；
   - 否则分配**全新** occurrence（并预占落账）。
3. `SideEffectStore.claim(...)`：把**无 owner 的 PENDING**视为可接管的预占（条件 UPDATE
   `claim_owner IS NULL` 原子夺取）；**有 owner 的 PENDING** 仍受租约保护，语义不变。

因此：

| 场景 | 行为 |
|---|---|
| 真重放 A₁（attempt1 只提出 `[A]`） | attempt2 解析到已成功的 occ0 -> 去重（安全） |
| A₁ 成功、恢复只提出 A₂（attempt1 提出 `[A, A]`） | attempt2 读到未执行的 occ1 -> **执行 A₂**（不再静默吞掉） |
| 完整重放 `[A₁, A₂]` | 两笔都解析回各自 occurrence -> 去重，零额外执行 |
| 重排恢复 `[B, A₂]` | 各自内容寻址 -> 不碰撞、不丢失 |
| 显式 `logical_operation_id` | 优先级最高，跨 occurrence 重置恒定（HITL 约定不变） |
| 预占槽位出现**不同参数** | 指纹冲突 -> fail-closed（不执行） |
| 同一作用域同内容 occurrence 超上限 | `OperationIdentityUnavailable` -> fail-closed（不静默新建） |

**未采用的伪修复**：不把 occurrence 改成全局计数器、不在恢复时无条件 +1、不按
`tool_name+arguments` 全局去重、不用随机 UUID、不把 `tool_call_id` 当业务 id、冲突时不
静默返回旧成功结果、不关闭幂等保护。仍然复用既有 ledger 唯一约束 / claim CAS /
fingerprint 冲突校验。

### 3.1 兼容与迁移

**无数据库 schema 变更、无破坏性迁移。** 预占复用现有 `tool_side_effects` 行（PENDING、
`claim_owner IS NULL`），键仍是既有 `v2:` 内容寻址键；`v1:` 位置键与
`run_id:tool_call_id`（HITL）键空间不变。旧数据（有 owner 的 PENDING / SUCCEEDED /
FAILED）语义完全不变。

## 4. 修复前 / 修复后证据

### 4.1 单元（scripted LLM 驱动**真实** `_process_with_tools` + 独立 SQLite ledger）

修复前（HEAD `e338446`，本专项新回归直接失败）：

```text
$ python -m pytest tests/unit/test_side_effect_partial_resume_identity.py -q
FAILED test_case_b_resume_only_a2_is_not_silently_swallowed
E   assert 1 == 2
1 failed, 8 passed
```

修复后：

```text
$ python -m pytest \
    tests/unit/test_side_effect_stable_logical_identity.py \
    tests/unit/test_side_effect_partial_resume_identity.py -q
...  (22 passed)      # 原 13 项 + 新增 9 项
```

关键量（Case B）：

- A₁ 实际执行次数：1
- A₂ 实际执行次数：修复前 0（静默吞掉）→ 修复后 1
- ledger：修复前仅 occ0=SUCCEEDED，occ1 从未落账；修复后 occ1 在 A₁ 执行前已作为预占
  落账，恢复时被 claim 并置 SUCCEEDED
- 恢复路径：修复前 `execute_idempotent_operation` 命中 K0 -> `CLAIM_SUCCEEDED` 直接返回；
  修复后解析到未执行 occ1 -> `CLAIM_EXECUTE`

### 4.2 全量单测 / 静态检查

```text
$ python -m pytest tests/unit -q
3477 passed, 5 skipped

$ ruff check .
All checks passed!
```

### 4.3 真实 PostgreSQL + Redis（本机真实依赖）

```text
$ TEST_DISTRIBUTED_DB_URL=... TEST_REDIS_URL=... \
    python -m pytest tests/integration/runtime/test_side_effect_replay_identity_pg.py -q
6 passed          # 含新增 partial_resume 用例（真实 PG 预占 + 恢复）

$ TEST_DISTRIBUTED_DB_URL=... TEST_REDIS_URL=... \
    python -m pytest tests/integration/runtime -q -p no:cacheprovider
199 passed

$ python scripts/test_worker_crash_recovery.py --output artifacts/runtime/chaos-*.json
[PASS] runtime-chaos

$ python scripts/verify_distributed_runtime.py
{"overall_status": "PASS", ...}
```

**Level 2 ≠ 生产验证**：`runtime-chaos` / `runtime-verify` 是真实基础设施 CI 证据，
不等于生产集群验证。

## 5. 残余边界（不得越界宣称）

- **端到端 exactly-once 仍为 NOT PROVIDED。** 本修复只保证本仓 ledger 的
  application-level 去重；下游外部系统若需去重仍须接受本仓传出的 idempotency key 或人工对账。
- **仍不可区分「无任何持久化痕迹的同参提案」**：若 attempt1 只提出过一次 A、attempt2
  再次提出 A，二者在系统内**完全等价**；此时按「重放」去重（与既有 ledger 存在理由一致）。
  需要区分时必须由调用方提供显式、持久的 `logical_operation_id`（HITL / 外部业务键）。
  **本修复把「可被持久化记录的第二次同参提案」从不丢失，但不宣称能把任意非确定 LLM
  输出自动映射回原始业务意图。**
- **参数须跨重放确定**：LLM 幻觉出新的 nonce/时间戳会被内容寻址当成另一笔。
- **HITL 语义未变**：`run_id:approval:{approval_id}` 保持不变；`/api/chat` 快路径无 run
  上下文，不在本治理边界内。
- 真实 ERP 写、生产集群、多副本长期稳定、multi-region 仍为 `NOT_VERIFIED`。

## 6. 复现

```bash
python -m pytest tests/unit/test_side_effect_stable_logical_identity.py \
                 tests/unit/test_side_effect_partial_resume_identity.py -q

TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \
TEST_REDIS_URL=redis://localhost:6379 \
  python -m pytest tests/integration/runtime/test_side_effect_replay_identity_pg.py -q
```
