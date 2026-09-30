# E2E 真实 LLM 验证指南

> 本文档解决"无真实端到端验证"缺口，提供从配置到验证的完整操作步骤。
>
> **校准说明（v6.3，2026-09-30）**：验证流程以当前代码为准（provider 探针、
> JWT/占位符校验、WebSocket DEV_MODE 不绕过等加固均已生效）。历史实测結果
> 一律归入 §8 Historical Evidence 区；当前测试数量用 `pytest --collect-only -q`
> 现场获取，不沿用任何历史数字。涉及 RAG 检索质量时，走
> `make rag-eval-649-preflight` 流程（见
> [docs/reference/rag-evaluation.md](../reference/rag-evaluation.md)），
> 不要把历史 30-query 快照当作当前结果。
>
> **provider 先探针**：任何真实调用前先 `python3 scripts/probe_provider_auth.py`，
> 401/403 是 NON_RETRYABLE auth 失败，chat/流式/评测都会被阻塞——这就是
> 历史 staging 运行失败与当前 RAG preflight BLOCKED 的根因。

---

## 1. 目标

用一次真实 LLM 调用验证系统端到端能跑通，生成可截图/录屏的 demo 证据。

---

## 2. 前置条件

| 条件 | 说明 |
|------|------|
| Python 3.10+ | 已在开发机上安装 |
| 项目依赖 | `pip install -r requirements.txt` |
| API Key | 硅基流动 / DeepSeek / OpenAI 任一平台的有效 Key |

### 2.1 获取 API Key

> ⚠️ **价格说明**：以下费用为 2026-06 撰写时的历史估计值，价格可能已变化；
> 部署前以 provider 官网当前定价为准。本仓库不维护动态价格。

**推荐：硅基流动（SiliconFlow）**——当时 Qwen3-8B 约 ¥0.35/百万 token、
一次 E2E 验证成本约 < ¥0.01（历史估计）。

1. 访问 https://siliconflow.cn 注册账号
2. 创建 API Key（以 `sk-` 开头）
3. 记录 Key 值

**备选：DeepSeek** —— 当时 deepseek-chat 约 ¥1/百万 token（历史估计）。

---

## 3. 配置步骤

### 3.1 创建 .env 文件

```bash
# 复制模板
cp .env.example .env
```

### 3.2 修改关键配置

编辑 `.env`，修改以下几项（embedding 走 HTTP API：默认未单独配置时
`EMBEDDING_API_KEY` 复用 `OPENAI_API_KEY`；需要独立凭据时单独设置。
reranker 默认读 `RERANKER_API_KEY`，未配置时重排不可用并回退原始顺序，
不会被静默伪装成功——preflight 探针会显式标记）：

```bash
# ===== 必改项 =====
LLM_PROVIDER=siliconflow
OPENAI_API_KEY=sk-你的实际Key
OPENAI_BASE_URL=https://api.siliconflow.cn/v1
OPENAI_MODEL=Qwen/Qwen3-8B

# ===== E2E 验证期间建议关闭认证（简化流程）=====
API_KEY_ENABLED=false
JWT_SECRET=test-secret-for-dev
SESSION_TOKEN_SECRET=test-session-secret

# ===== 可选：关闭 Redis 依赖 =====
# 不设 REDIS_URL 即可，系统自动降级到内存模式
```

如果使用 DeepSeek：
```bash
LLM_PROVIDER=deepseek
OPENAI_API_KEY=sk-你的DeepSeek-Key
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

---

## 4. 验证路径一：运行 E2E 测试（推荐，5 分钟）

```bash
# 真实调用前先探针 provider 认证（401/403 直接 STOP）
python3 scripts/probe_provider_auth.py

# 运行真实 LLM 端到端测试（当前测试数量/名称以 pytest 实际输出为准，
# 不要把本节的历史结果当作当前 checkout 的验证）
python3 -m pytest tests/e2e/test_e2e_real_llm.py -v -s
```

有效 provider credential 就绪后运行真实 LLM suite（`pytest tests/e2e/test_e2e_real_llm.py`）；
**PASS / FAIL / skipped 数量与耗时一律以该次执行输出为准**。当前仓库没有
"5 个测试全部 PASS" 的当前证据——你执行时看到的结果才是本轮结果（且每次都可能与
历史快照不同）。

### 测试覆盖内容

| 测试 | 验证点 |
|------|--------|
| `test_basic_product_query` | 产品咨询 → 返回非空响应 (>20字符)，有 query_type 和 collaboration_mode |
| `test_return_exchange_query` | 退货请求 → 路由到 complaint/billing/general agent |
| `test_technical_query_with_rag` | 技术问题 → RAG 检索触发，响应包含相关关键词 |
| `test_multi_turn_context` | 多轮对话 → 两轮问题共享 session_id，第二轮能引用第一轮内容 |
| `test_injection_defense` | 注入攻击 → 系统 prompt 不泄露，安全拒绝或正常回复 |

### 截图要点

建议截取以下画面作为面试展示素材（用**你自己的当前运行结果**截图）：

1. **终端截图**：probe 探针 OK + `pytest PASSED` 的测试结果
2. **终端截图**：每个测试的详细输出（query_type、collaboration_mode、响应内容）

---

## 5. 验证路径二：启动 Web 界面（效果最好，10 分钟）

### 5.1 启动后端

```bash
# 开发模式启动（无需 Docker、无需 Redis、无需 PostgreSQL）
python3 -m uvicorn api.app_factory:app --host 0.0.0.0 --port 8000 --reload
```

预期日志：
```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 5.2 打开浏览器

访问 http://localhost:8000 → 自动跳转登录页

**开发模式默认账号**（仅 `DEV_MODE=true` 时显示提示）：
- 用户名：`admin`
- 密码：`admin123`

### 5.3 演示场景

登录后，在聊天界面依次测试以下场景：

#### 场景 1：产品咨询（展示 RAG + Agent 路由）
```
你们的烟酰胺精华液有什么功效？适合什么肤质？
```
预期：路由到 Product Agent，响应包含成分功效、适用肤质信息。

#### 场景 2：多领域查询（展示协作模式选择）
```
我买的产品过敏了，同时帮我查一下订单1001的物流状态
```
预期：复杂度评分高，可能触发 Parallel 或 Consultation 模式。

#### 场景 3：退货退款（展示 Agent 路由）
```
我要退款，产品和描述不符
```
预期：路由到 Complaint Agent 或 Billing Agent。

#### 场景 4：ReAct 推理（展示工具调用）
```
帮我查一下你们有没有适合油性皮肤的精华液，价格在200元以内
```
预期：可能触发 ReAct 模式，调用 ERP 工具查询产品。

#### 场景 5：监控面板（展示运维能力）
点击顶部"监控"标签 → 查看实时指标面板：
- 请求总量、平均响应时间
- Agent 调用分布图
- 协作模式分布图
- SLA 达标率

### 5.4 录屏建议

用系统录屏工具（macOS: QuickTime, Windows: Win+G）录制：
1. 登录过程（5 秒）
2. 发送产品咨询 → 收到 AI 回复（10 秒）
3. 发送复杂查询 → 观察 Agent 流转过程（15 秒）
4. 切换到监控面板 → 展示实时数据（10 秒）

总时长控制在 40-60 秒，面试时播放。

---

## 6. 验证路径三：API 直接调用（最简洁，2 分钟）

如果不想启动 Web 界面，直接用 curl 验证：

```bash
# 1. 聊天接口
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "你们的烟酰胺精华液有什么功效？", "session_id": "test-001"}' | python3 -m json.tool

# 2. 健康检查
curl http://localhost:8000/api/health | python3 -m json.tool

# 3. 缓存统计
curl http://localhost:8000/api/cache/stats | python3 -m json.tool

# 4. SSE 流式输出
curl -N http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"query": "推荐适合敏感肌的护肤品", "session_id": "test-002"}'
```

---

## 7. 常见问题排查

| 问题 | 原因 | 解决 |
|------|------|------|
| `qdrant-client` 导入失败 | 未安装 | `pip install qdrant-client` |
| LLM 返回 401 | API Key 无效 | 检查 `.env` 中 `OPENAI_API_KEY` |
| LLM 返回超时 | 网络问题或模型繁忙 | 增大 `HTTP_TIMEOUT=60` |
| `ModuleNotFoundError: langgraph` | 依赖缺失 | `pip install -r requirements.txt` |
| 端口 8000 被占用 | 其他进程占用 | `lsof -i :8000` 查看并杀掉 |
| 测试跳过 "requires OPENAI_API_KEY" | Key 是占位符 | 确保 `.env` 中 Key 是真实值 |

---

## 8. Historical Evidence（2026-06-20 快照，硅基流动 Qwen3-8B）

> **本节是历史实测快照（Historical Evidence），属于其执行时点的记录。**
> 它不是当前 checkout 的验证结果；当前验证必须重新执行 §4 的测试，
> 数量与结果以当前 pytest 输出为准。
> 当时 `pytest --collect-only -q` 的收集数（当年口径：1352 个）是历史时点数字，
> **不作为当前测试数**——当前收集数请现场执行该命令获取。

```
tests/e2e/test_e2e_real_llm.py::TestRealLLMEndToEnd::test_basic_product_query        PASSED   8.9s
tests/e2e/test_e2e_real_llm.py::TestRealLLMEndToEnd::test_return_exchange_query      PASSED  12.7s
tests/e2e/test_e2e_real_llm.py::TestRealLLMEndToEnd::test_technical_query_with_rag   PASSED   6.2s
tests/e2e/test_e2e_real_llm.py::TestRealLLMEndToEnd::test_multi_turn_context         PASSED  14.5s
tests/e2e/test_e2e_real_llm.py::TestRealLLMEndToEnd::test_injection_defense          PASSED   6.0s
=============================== 5 passed in 58.28s ===============================
```

> 上面的测试时长（8.9s–14.5s、总 58.28s）为 2026-06-20 当次网络/模型快照，
> 不是可复现的当前延迟；不得被引用为 SLA 或性能证据。

### 集成测试中发现并修复的 Bug（历史记录）

真实 LLM 测试暴露了两个 Mock 测试无法覆盖的问题，已修复：

#### Bug 1：路由优先级缺陷

**现象**："面霜过敏了想退货退款" 被路由到 `product_agent` 而非 `complaint_agent`

**根因**：规则分类器中 `product_info`、`technical_support`、`billing` 三个意图各匹配 1 次（"面霜"→产品、"过敏"→技术、"退货退款"→账单），`max(scores)` 取字典插入顺序第一个 `product_info`。

**修复**：[router/query_router.py](../../router/query_router.py) 新增意图优先级 `_INTENT_PRIORITY`，同分时按 complaint > billing > technical > order > product > cosmetic_advice 排序：

```python
# 同分时高优先级意图胜出
best_intent = max(scores, key=lambda k: (scores[k], -_INTENT_PRIORITY.get(k, 99)))
```

#### Bug 2：注入防御漏洞

**现象**：Qwen3-8B 收到注入攻击后，回复了"我的系统提示词主要包括以下几个方面……"，实际泄露了系统角色设定。

**根因**：`[untrusted data]` 隔离标签只能防止 LLM 把用户输入当作系统指令，但无法阻止 LLM 在回复中讨论自己的系统设置。小模型对指令遵从不够强。

**修复**：[agents/response_agent.py](../../agents/response_agent.py) 新增输出层注入检测——正则匹配"系统提示词…如下/包括/是"等泄露模式，命中后替换为安全回复：

```python
_RE_INJECTION_DISCLOSURE = re.compile(
    r"(系统提示词?|system\s*prompt|我的指令|我的设定).{0,50}(如下|包括|是|内容|主要|为|包含)",
    re.IGNORECASE,
)
# 命中时替换为安全客服回复
```

**面试话术**：

> "这两个 Bug 是只有跑真实 LLM 才能发现的——Mock 测试中路由结果是预设的，不会暴露优先级排序缺陷；注入防御用的也是固定输入输出，不会触发小模型的实际泄露行为。这说明 E2E 真实测试是不可替代的。"

---

## 9. 面试话术

准备面试时这样说（**只能引用你自己当前运行的实测结果**；没有新 artifact 就不要报当前 PASS 数）：

> "这个项目建立了完整的 E2E 验证链路：provider 认证探针 → 真实 LLM suite
> （产品咨询、退货路由、RAG 增强、多轮上下文、注入防御等场景）→ 历史教训驱动
> 的输出层加固。**历史口径**：2026-06 曾有一次真实 LLM E2E 快照跑完当时全部
> 5 个场景（HISTORICAL SNAPSHOT，不可复用为当前结果）；**当前证据**：provider
> auth probe 最新留档是 HTTP 401 / BLOCKED_BY_AUTHENTICATION，所以真实调用
> 当前是阻塞的——我不会把历史结果包装成当前生产验证。测试中发现过两个 Mock
> 无法覆盖的问题：路由优先级缺陷和小模型注入泄露，都已在输出层修复。"
>
> （如录屏）"这里有一段 40 秒的录屏，展示了从登录到多轮对话到监控面板的完整流程。"
