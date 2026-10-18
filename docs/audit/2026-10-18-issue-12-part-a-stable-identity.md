# Issue #12 Part A — 稳定逻辑操作身份（内容寻址）设计与证据（2026-10-18）

> **对齐报告（HISTORICAL AUDIT SNAPSHOT）**。绑定 PR 修复时的代码与测试；结合
> [reference/current-state.md](../reference/current-state.md) 与
> [reference/runtime-state-convergence.md](../reference/runtime-state-convergence.md) 阅读。
> 证据状态词：`VERIFIED_LOCAL`（本地真实执行） / `NOT_VERIFIED`（未验证） / `NOT PROVIDED`（不提供）。

## 1. 问题（Part A）

副作用幂等键 `tool_side_effects.operation_key` 过去由
`run_id + node_id + agent + tool_name + ordinal` 派生，而 `ordinal` 是「调用序列里的
**位置**」。节点在 LangGraph checkpoint 提交前崩溃并被重放时，LLM 会被重新调用；若它
把**同一写工具**的调用序列重排（插入/交换另一次同类写），同一笔写会换到另一个槽位、
拿到**新的 operation_key**，而 `request_fingerprint` 只在同一个键内比较，看不到这次
重复 —— 副作用**可能被重复执行**。

可达的最小形态（`refund(A)` 已是槽位 0）：

```
attempt 1: [refund(A)]        -> refund(A) @ ordinal=0 -> key K0 -> 执行 A（SUCCEEDED）
attempt 2: [refund(B), refund(A)] -> refund(A) @ ordinal=1 -> key K1 -> A 被再次执行
```

（只读工具不影响槽位：计数器按工具名分槽；因此缺陷只在**同一写工具**的调用序列重排时
可达，Issue #12 正文已更正这一点。）

## 2. 修复设计

身份改为**内容寻址**，与调用在序列里的位置无关：

```
scope   = run_id | node_id | agent | tool_name
identity = sha256("v2|" + scope + "|content:" + sha256(canonical_json(arguments))
                          + "|occ:" + occurrence)
         # 或调用方显式：sha256("v2|" + scope + "|id:" + logical_operation_id)
key       = "v2:" + identity
```

- **内容**（`sha256(canonical_json(arguments))`）：同一笔写无论排到第几位都命中同一个
  键，重排不再产生新身份。
- **同内容出现序号**（`occurrence`）：只区分**参数完全相同的有意重复**（用户两次各退款
  100 元），按 `(tool_name, 内容指纹)` 分槽，故插入/交换**其它**调用不会改变它。
- **显式 `logical_operation_id`**（首选）：编排层持有稳定业务操作 id 时直接传它，身份
  完全来自调用方，参数内容不参与派生。
- **位置寻址**（`v1`，`ordinal` 当位置用）：仅保留给「只有位置」的 legacy 调用方；
  Agent 工具循环**不**再走它。

三个约束的取舍：

| 要求 | 设计如何满足 |
|---|---|
| 同一笔写跨 round/retry/replay/resume 身份稳定 | 身份只由内容（+出现序号）或显式 id 决定，不含 LLM 输出、不含位置 |
| 不同业务写操作可区分 | 作用域四维 + 内容指纹 + 出现序号，任一不同即不同键 |
| 参数相同的有意重复不被合并 | 出现序号按内容分槽递增 -> 不同键 -> 都执行 |
| 不依赖 LLM `tool_call_id` | 键派生完全不引用 `tool_call_id` |
| 缺少稳定身份时 fail-closed | 无 `logical_call` 且无稳定 `tool_call_id` -> registry 拒绝执行 |

## 3. 关键调用链

```
agents/base_agent.py::_process_with_tools
  └─ _content_occurrence(tool_name, args)      # 按 (tool, 内容指纹) 分槽，跨 LLM round 不重置
       -> LogicalCall(node_id=f"{agent}#{invocation}", agent, ordinal=occurrence)
            │
tools/tool_registry.py::_idempotent_operation
  └─ build_replay_stable_operation_key(
         run_id, node_id, agent, tool_name,
         ordinal=logical_call.ordinal,        # 同内容出现序号
         logical_operation_id=...,            # 可选，首选
         arguments=arguments,                 # 内容寻址分量
     )
            │
runtime/side_effects.py::execute_idempotent_operation
  └─ claim(operation_key, fingerprint=sha256(json(args)))  # 唯一约束 + 行锁 + 条件 UPDATE
```

`node_id` 仍取「agent 名 + 本 run 内该 agent 的调用序号」（`state["agents_used"]`，是
checkpointed state，跨重放稳定）。

## 4. 修复前 / 修复后证据

**修复前（HEAD `36bcf20`，在临时 worktree 中跑新回归）**

```
$ pytest tests/unit/test_side_effect_stable_logical_identity.py \
      -k "reorder_does_not_reexecute"
E   AssertionError: A 换槽位后必须命中同一个 operation_key 被去重；A 出现两次说明身份仍是位置而非内容（Issue #12 Part A）
E   assert 2 == 1
E    +  where 2 = [{'amount': 100, 'order_id': 'ORD-A'}, {'amount': 100, 'order_id': 'ORD-A'}].count
```

`refund(A)` 在重放中被真实执行两次（`[A, A]`）。

**修复后**

```
$ pytest tests/unit/test_side_effect_stable_logical_identity.py -q      # 13 passed
$ pytest tests/unit/test_side_effect_ordinal_scope.py \
         tests/unit/test_side_effect_replay_wiring.py \
         tests/unit/test_side_effect_claim_floor_wiring.py \
         tests/unit/test_runtime_support.py -q                          # 17 passed
```

关键回归（`tests/unit/test_side_effect_stable_logical_identity.py`）：

- `test_same_tool_reorder_does_not_reexecute`：`[A] -> [B, A]`，A 只执行一次、B 执行一次。
- `test_node_replay_with_same_sequence_dedupes` / `test_replay_read_inserted_before_write_dedupes`。
- `test_repeated_redelivery_after_terminal_success_stays_stable`：终态后多次重投稳定。
- `test_identical_args_twice_in_one_node_are_two_operations` /
  `test_identical_args_across_two_rounds_are_two_operations`：同参数有意重复都执行。
- `test_explicit_logical_operation_id_separates_identical_args` /
  `test_explicit_logical_operation_id_both_execute`：显式 id 区分同参数不同意图。
- `test_positional_identity_versus_content_identity_on_reorder`：固定缺陷机制（位置身份
  下 A 换槽位拿到不同键）。
- `test_no_identity_refuses_write`：缺少身份 -> fail-closed。

## 5. 残余边界（不得越界宣称）

- **端到端 exactly-once NOT PROVIDED。** 本修复只保证本仓 ledger 的 application-level
  去重；下游外部系统若需去重，仍须接受本仓传出的 idempotency key（或人工对账）。
- **部分恢复（同参不同业务操作）残差另有专项修复（2026-10-18）。** 内容寻址身份在当时
  仍依赖「节点局部 occurrence 计数器」；worker 在「两笔同参写只执行了第一笔」后崩溃并
  整节点重放、且恢复只提出第二笔时，第二笔会被重新编号到 occurrence 0 撞上第一笔的成功
  键被静默吞掉。该残差已由持久化 occurrence 预占 + 恢复解析修复，证据见
  [2026-10-18-issue-12-part-a-partial-resume-identity.md](2026-10-18-issue-12-part-a-partial-resume-identity.md)。
- **参数须跨重放确定。** 若 LLM 在重放时对同一笔写产生新的 nonce/时间戳，内容寻址会
  把它当成另一笔操作。这是内容寻址的固有边界，不是本仓缺陷。
- **HITL 语义未变。** `operation_key = run_id:approval:{approval_id}` 保持不变。
- **真实 ERP 写、生产集群、多副本长期稳定、multi-region 仍为 `NOT_VERIFIED`。**

## 6. 复现

```bash
python -m pytest tests/unit/test_side_effect_stable_logical_identity.py -q
python -m pytest tests/unit/test_side_effect_ordinal_scope.py \
                 tests/unit/test_side_effect_replay_wiring.py -q
# 真实 PG（需 TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL）
TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \
TEST_REDIS_URL=redis://localhost:6379 \
  python -m pytest tests/integration/runtime/test_side_effect_replay_identity_pg.py -q
```
