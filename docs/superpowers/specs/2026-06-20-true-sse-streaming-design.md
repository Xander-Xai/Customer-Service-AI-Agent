# 全链路真流式 SSE Streaming 设计文档

> **HISTORICAL DESIGN SNAPSHOT (2026-06-20)**
> 这是带日期的设计文档，记录作者当时的设计意图，**不是当前架构权威**。
> Current Truth: `docs/reference/current-state.md`；仅作历史留档。

> 日期：2026-06-20
> 状态：设计阶段
> 影响范围：api/routes/chat.py, agents/base_agent.py, core/graph_builder.py, rag/, web/src/

## 1. 动机与目标

当前 SSE 流式在非 Tool-Calling 路径上已实现 token-by-token 输出，但存在三个关键缺口：

1. **Tool-Calling 轮次完全阻断流式**：RAG 检索、订单查询等工具执行期间用户看到屏幕完全静止
2. **图节点执行无状态反馈**：用户不知道系统当前在处理什么（缓存检查？分类？路由？）
3. **缓存命中无流式体验**：L1/L2 命中直接返回完整 `done` 事件，跳过所有输出动画

### 成功标准

- Tool-Calling 轮次期间，前端能实时看到 Agent 的思考过程和工具调用状态
- Graph 各节点（check_cache → classify_query → route_to_mode → agent）各阶段 emit 可见状态
- 缓存命中时以伪流式分块输出，维持一致的用户体验
- RAG 检索各阶段（改写→检索→重排→完成）emit 详细状态
- 所有新增 event types 在前端有对应的渲染组件
- 新增代码测试覆盖 ≥80%

## 2. SSE Event Types 扩展

现有 → 扩展：

| Event Type | 来源 | 用途 | 前端渲染 |
|---|---|---|---|
| `chunk` | LLM 输出 token | 逐 token 追加内容 | 追加到 .streaming-text |
| `done` | 图执行完成 | 最终事件，携带完整元数据 | 替换为渲染后的 Markdown |
| `error` | 异常 | 错误信息 | 错误提示 |
| `status` | **新增** | 图各节点状态更新 | 顶部状态栏，带渐出 |
| `thinking` | **新增** | Agent 推理过程 | 灰色斜体可折叠块 |
| `tool_call` | **新增** | 工具调用 | 工具名 + 参数卡片 |
| `tool_result` | **新增** | 工具返回摘要 | 可折叠详情卡片 |
| `rag_status` | **新增** | RAG 检索各阶段 | 带步骤指示器的状态行 |
| `agent_switch` | **新增** | Agent 切换 | 过渡标签 |

### 事件格式约定

```json
{"type": "status",      "phase": "cache|classify|route", "content": "🔍 检查缓存中..."}
{"type": "thinking",    "content": "🤔 分析中...",        "agent": "product"}
{"type": "tool_call",   "name": "search_kb",              "args": {...},         "agent": "product"}
{"type": "tool_result", "name": "search_kb",              "summary": "找到 3 条", "agent": "product"}
{"type": "rag_status",  "status": "rewriting|retrieving|reranking|done", "query": "...", "count": 5}
{"type": "agent_switch","from": "router",                 "to": "product"}
```

## 3. 模块设计

### 3.1 P0: Tool-Calling 流式化

**文件：** `agents/base_agent.py`

**改动：** `_process_with_tools()` 方法

**逻辑流程：**

```
用户问题
  → [emit thinking]  "🤔 分析中，需要查询知识库..."
  → [emit tool_call]  {"search_knowledge_base", args}
  → async_invoke (获取 tool_calls — 需完整 JSON)
  → 执行工具，各阶段 [emit rag_status/tool_result]
  → [emit thinking]  "💡 根据检索结果生成回答..."
  → async_invoke_stream → [emit chunk × N]
```

**关键设计决策：**

- 首次 LLM 调用仍然用 `async_invoke`（非流式），因为 tool_calls 需要完整 JSON
- 只有最终响应走 `async_invoke_stream`
- `_execute_tool()` 扩展签名，支持注入 `stream_callback`
- 工具函数通过 inspect 检测 `stream_callback` 参数，存在则注入

### 3.2 P1: 图节点状态流式 + 缓存伪流式

**文件：** `core/graph_builder.py`, `cache/response_cache.py`

**改动：** 抽取 `_emit_status(state, phase, message)` 工具函数，各节点入口调用

**各节点 emit 点：**

| 节点 | emit | 条件 |
|---|---|---|
| check_cache | `status:cache` + "🔍 检查缓存中..." | 总是 |
| check_cache | `status:cache` + "L1 未命中，进行语义匹配" | L1 miss |
| check_cache | `chunk` × N（伪流式） | 缓存命中 |
| classify_query | `status:classify` + "分类结果: {category}" | 分类完成 |
| route_to_mode | `status:route` + "协作模式: {mode}" | 路由完成 |
| agent switch | `agent_switch` + from→to | 节点切换 |

**缓存伪流式参数：** chunk_size=20, interval=0.03s

### 3.3 P2: RAG 检索状态流式

**文件：** `rag/` 工具函数（如 `tools/search_knowledge_base`）

**改造：** 工具函数签名增加可选 `stream_callback` 参数，在 4 个阶段 emit `rag_status`：

1. `rewriting` — 查询改写
2. `retrieving` — Qdrant 检索（v6.0 从 ChromaDB 迁移）
3. `reranking` — BM25/CrossEncoder 重排
4. `done` — 完成

### 3.4 前端渲染

**文件：** `web/src/`

**新增事件处理器：**

| 事件 | 渲染逻辑 |
|---|---|
| `status` | 在消息输入框上方显示短暂的渐出状态条 |
| `thinking` | 在消息气泡内添加灰色斜体文本块，可折叠 |
| `tool_call` | 卡片样式，显示工具名 + 参数折行 |
| `tool_result` | 可折叠卡片，显示摘要，可展开看详情 |
| `rag_status` | 行内步骤指示器（改写→检索→重排→完成） |
| `agent_switch` | 消息上方 agent 标签过渡动画 |

## 4. 完整数据流示例

```
用户: "烟酰胺和VC能一起用吗？"

SSE 输出（按时间顺序）:

1. {"type":"status",     "phase":"cache",   "content":"🔍 检查缓存中..."}
2. {"type":"status",     "phase":"cache",   "content":"🔍 L1 未命中，进行语义匹配..."}
3. {"type":"status",     "phase":"classify", "content":"📋 正在分类问题..."}
4. {"type":"status",     "phase":"classify", "content":"📋 分类结果: product"}
5. {"type":"status",     "phase":"route",    "content":"🔄 协作模式: sequential"}
6. {"type":"agent_switch","from":"router",   "to":"product"}
7. {"type":"thinking",   "content":"🤔 产品 Agent 正在分析，需要检索知识库了解成分信息...", "agent":"product"}
8. {"type":"tool_call",  "name":"search_knowledge_base", "args":{"query":"烟酰胺 VC 相互作用 禁忌"}, "agent":"product"}
9. {"type":"rag_status", "status":"rewriting",  "query":"烟酰胺 维生素C 相互作用", "agent":"rag"}
10.{"type":"rag_status", "status":"retrieving", "query":"烟酰胺 维生素C 相互作用", "agent":"rag"}
11.{"type":"rag_status", "status":"reranking",  "count":10, "agent":"rag"}
12.{"type":"rag_status", "status":"done",       "count":3,  "agent":"rag"}
13.{"type":"tool_result","name":"search_knowledge_base", "summary":"找到 3 条相关结果", "agent":"product"}
14.{"type":"thinking",   "content":"💡 已获取成分信息，正在生成对比回答...", "agent":"product"}
15.{"type":"chunk",      "content":"烟"}
16.{"type":"chunk",      "content":"酰"}
17.{"type":"chunk",      "content":"胺"}
   ... (逐 token)
18.{"type":"done",  "content":"完整Markdown回答...", "agent":"product", "mode":"sequential", "cached":false}
```

## 5. 错误处理

- `stream_callback` 内部异常必须被 `try/except` 捕获，不影响主流程
- 如果 `stream_callback` 抛出异常，记录 warn 日志并继续执行
- 工具函数的 `stream_callback` 参数为可选（通过 inspect 检测），向后兼容

## 6. 测试策略

| 测试 | 范围 | 内容 |
|---|---|---|
| 单元测试 | agents/base_agent.py | `_process_with_tools()` emit 正确的事件序列 |
| 单元测试 | core/graph_builder.py | 各节点在流式/非流式模式下行为正确 |
| 单元测试 | rag/ 工具函数 | 带/不带 stream_callback 的行为 |
| 单元测试 | 缓存伪流式 | chunk_size, interval, 空内容边界 |
| 集成测试 | api/routes/chat.py | SSE generator 输出完整 event 序列 |
| 前端测试 | web/src/api/sse.js | 新 event types 的分发逻辑 |
| 前端测试 | web/src/chat/messages.js | thinking/tool_call/rag_status 渲染 |

## 7. 实施计划

1. **P0-1**: `_emit_status()` 工具函数 → 图节点注入 status emit
2. **P0-2**: 缓存伪流式（check_cache 节点改造）
3. **P0-3**: `_process_with_tools()` 流式化
4. **P0-4**: `_execute_tool()` 扩展 + inspect 注入
5. **P1**: `rag_status` 支持 + 搜索知识库工具改造
6. **P2**: 前端新事件 types 渲染
7. **P3**: 测试
