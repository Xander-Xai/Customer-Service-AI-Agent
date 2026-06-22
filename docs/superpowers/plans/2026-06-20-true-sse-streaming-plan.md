# 全链路真流式 SSE Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement true end-to-end SSE streaming across the entire chain — graph node status, cache pseudo-streaming, tool-calling reasoning visibility, and RAG retrieval status.

**Architecture:** Extend the existing `stream_callback` mechanism with new SSE event types (`status`, `thinking`, `tool_call`, `tool_result`, `rag_status`, `agent_switch`). Inject callbacks at key points: graph nodes (cache/classify/route), tool execution (`_process_with_tools`), and tool handler functions. Frontend renders each new event type with distinct UI elements.

**Tech Stack:** Python 3.10+ / FastAPI / LangGraph / Qdrant (v6.0 从 ChromaDB 迁移) / vanilla JS SSE

---

### Task 1: Graph Node Status Emit Helper

**Files:**
- Modify: `core/graph_builder.py` — add `_emit_status()` helper + inject into `_check_cache_node` and `_classify_query_node`
- Test: `tests/unit/test_graph_builder.py`

- [ ] **Step 1: Write test for `_emit_status` behavior**

```python
# tests/unit/test_graph_builder.py
# Add after existing imports

@pytest.mark.unit
async def test_emit_status_sends_events():
    """验证 _emit_status 辅助函数正确发出 status SSE 事件"""
    from core.graph_builder import _emit_status

    events = []
    async def mock_cb(event):
        events.append(event)

    state = {"stream_callback": mock_cb}

    await _emit_status(state, "cache", "🔍 检查缓存中...")
    assert len(events) == 1
    assert events[0] == {"type": "status", "phase": "cache", "content": "🔍 检查缓存中..."}

@pytest.mark.unit
async def test_emit_status_no_callback():
    """stream_callback 不存在时不报错"""
    from core.graph_builder import _emit_status

    state = {}  # no stream_callback
    await _emit_status(state, "cache", "test")  # should not raise

@pytest.mark.unit
async def test_emit_status_callback_error_logged():
    """callback 异常时不应传播，只 log 并继续"""
    from core.graph_builder import _emit_status

    state = {"stream_callback": lambda x: (_ for _ in ()).throw(Exception("boom"))}
    await _emit_status(state, "cache", "test")  # should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_graph_builder.py::test_emit_status_sends_events tests/unit/test_graph_builder.py::test_emit_status_no_callback tests/unit/test_graph_builder.py::test_emit_status_callback_error_logged -v`
Expected: FAIL with ImportError for `_emit_status`

- [ ] **Step 3: Add `_emit_status` helper to graph_builder.py**

Add after the `_format_duration` function (around line 37):

```python
async def _emit_status(state: AgentState | dict, phase: str, message: str) -> None:
    """图节点状态 emit 辅助函数（v6.0: 全链路 SSE 流式）
    
    从 state 中获取 stream_callback，发出 status 事件。
    回调不存在或抛出异常时不传播，仅 debug log。
    """
    cb = state.get("stream_callback") if isinstance(state, dict) else None
    if cb:
        try:
            await cb({"type": "status", "phase": phase, "content": message})
        except Exception:
            logger.debug(f"_emit_status: callback failed (phase={phase})", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_graph_builder.py::test_emit_status_sends_events tests/unit/test_graph_builder.py::test_emit_status_no_callback tests/unit/test_graph_builder.py::test_emit_status_callback_error_logged -v`
Expected: PASS

- [ ] **Step 5: Inject `_emit_status` into `_check_cache_node`**

In `core/graph_builder.py`, modify the `_check_cache_node` function (starts around line 184):

```python
async def _check_cache_node(state: AgentState) -> AgentState:
    """缓存检查节点（v6.0: SSE 状态流式 + 缓存伪流式）"""
    query = state["customer_query"]
    
    await _emit_status(state, "cache", "🔍 检查缓存中...")
    
    cached = c.cache.get(query)
    if cached:
        await _emit_status(state, "cache", "⚡ 缓存命中，快速响应中...")
        
        # v6.0: 缓存伪流式 — 分块输出缓存内容
        stream_callback = state.get("stream_callback")
        if stream_callback:
            chunk_size = 20
            interval = 0.03
            for i in range(0, len(cached), chunk_size):
                try:
                    await stream_callback({"type": "chunk", "content": cached[i:i+chunk_size]})
                except Exception:
                    pass
                await asyncio.sleep(interval)
        
        state["response"] = cached
        state["cached"] = True
        state["current_agent"] = "cache"
        state["collaboration_mode"] = "cache_hit"
        logger.info(f"[Cache] HIT: {query[:30]}...")
    else:
        await _emit_status(state, "cache", "🔍 L1 未命中，进行语义匹配...")
        state["cached"] = False
        logger.debug(f"[Cache] MISS: {query[:30]}...")
    return state
```

- [ ] **Step 6: Inject `_emit_status` into `_classify_query_node`**

In `core/graph_builder.py`, inside `_classify_query_node`:
- After `set_trace_id(trace_id)` (around line 77), add:
  ```python
  await _emit_status(state, "classify", "📋 正在分类问题...")
  ```
- Before `return state` (around line 158), after routing result is stored:
  ```python
  await _emit_status(state, "classify", f"📋 分类结果: {result.query_type} (agent={result.agent_name})")
  ```

- [ ] **Step 7: Add `agent_switch` + mode emit in `_execute_collaboration`**

In `_execute_collaboration` (around line 202), after routing_result is built and before `mode.execute(...)`:

```python
    # v6.0: emit agent_switch 和 mode 事件
    agent_name = routing_result.agent_name
    cb = state.get("stream_callback")
    if cb:
        try:
            await cb({"type": "status", "phase": "route", "content": f"🔄 协作模式: {mode_name}"})
            await cb({"type": "agent_switch", "from": "router", "to": agent_name})
        except Exception:
            pass
```

- [ ] **Step 8: Commit**

```bash
git add core/graph_builder.py tests/unit/test_graph_builder.py
git commit -m "feat(graph): graph node SSE status emit + cache pseudo-streaming"
```

---

### Task 2: SSE Generator Forward All Event Types

**Files:**
- Modify: `api/routes/chat.py` — `_sse_stream_generator()` forward new event types

- [ ] **Step 1: Modify `_sse_stream_generator` to forward all dict events**

In `api/routes/chat.py`, lines 176-177, change:

```python
                if isinstance(event, dict) and event.get("type") == "chunk":
                    yield _sse_event(event)
```

To:

```python
                if isinstance(event, dict) and event.get("type") in (
                    "chunk", "status", "thinking", "tool_call",
                    "tool_result", "rag_status", "agent_switch",
                ):
                    yield _sse_event(event)
```

- [ ] **Step 2: Commit**

```bash
git add api/routes/chat.py
git commit -m "feat(api): SSE generator forward all streaming event types"
```

---

### Task 3: _process_with_tools Streaming — Thinking + Tool Call Events

**Files:**
- Modify: `agents/base_agent.py` — `_process_with_tools()` emit thinking/tool_call/tool_result events
- Test: `tests/unit/test_base_agent_streaming.py` (new file)

- [ ] **Step 1: Write tests for `_process_with_tools` streaming events**

```python
# tests/unit/test_base_agent_streaming.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.base_agent import BaseAgent
from core.config import TOOL_MAX_ROUNDS


@pytest.mark.unit
async def test_process_with_tools_emits_thinking_and_tool_call():
    """_process_with_tools 在工具调用时 emit thinking + tool_call + tool_result 事件"""
    agent = BaseAgent(name="test_agent")
    agent.logger = MagicMock()
    
    events = []
    async def mock_cb(event):
        events.append(event)
    
    # Mock LLM — first call returns tool_calls, second returns final content
    mock_first_response = MagicMock()
    mock_first_response.tool_calls = [
        {"id": "call_1", "name": "query_product", "arguments": '{"keyword": "烟酰胺"}'}
    ]
    mock_first_response.content = ""
    
    mock_second_response = MagicMock()
    mock_second_response.tool_calls = None
    mock_second_response.content = "烟酰胺是一种维生素B3衍生物。"
    
    mock_llm = AsyncMock()
    mock_llm.async_invoke = AsyncMock(side_effect=[mock_first_response])
    mock_llm.async_invoke_stream = AsyncMock()
    mock_llm.async_invoke_stream.return_value.__aiter__.return_value = iter(["烟", "酰", "胺"])
    agent.llm = mock_llm
    
    # Mock tool_registry
    mock_registry = AsyncMock()
    mock_registry.get_openai_tools.return_value = [MagicMock()]
    mock_registry.execute = AsyncMock(return_value="成分说明：烟酰胺…")
    agent.tool_registry = mock_registry
    
    # Mock base methods
    agent._resolve_prompt_for_variant = MagicMock(return_value=("prompt", "control", None))
    agent._prepare_llm_messages = AsyncMock(return_value=("sid", [], False))
    agent._add_message_to_session = AsyncMock()
    agent._publish_event = AsyncMock()
    agent._try_rule_fallback = AsyncMock(return_value=None)
    
    state = {
        "customer_query": "烟酰胺是什么？",
        "stream_callback": mock_cb,
        "session_id": "test-session",
    }
    
    with patch.object(agent, '_process_with_tools', wraps=agent._process_with_tools) as spy:
        result = await agent._process_with_tools(state, "你是一个产品专家")
    
    event_types = [e["type"] for e in events]
    assert "thinking" in event_types, f"缺少 thinking 事件, got {event_types}"
    assert "tool_call" in event_types, f"缺少 tool_call 事件, got {event_types}"
    assert "tool_result" in event_types, f"缺少 tool_result 事件, got {event_types}"
    
    tool_call_events = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_call_events) >= 1
    assert tool_call_events[0]["name"] == "query_product"
    
    tool_result_events = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_result_events) >= 1
    assert "成分说明" in tool_result_events[0]["summary"]


@pytest.mark.unit
async def test_process_with_tools_no_tool_call_no_extra_events():
    """_process_with_tools 没有 tool_call 时不应 emit tool_call/tool_result"""
    agent = BaseAgent(name="test_agent")
    agent.logger = MagicMock()
    
    events = []
    async def mock_cb(event):
        events.append(event)
    
    mock_response = MagicMock()
    mock_response.tool_calls = None
    mock_response.content = "直接回答"
    
    mock_llm = AsyncMock()
    mock_llm.async_invoke = AsyncMock(return_value=mock_response)
    agent.llm = mock_llm
    
    mock_registry = AsyncMock()
    mock_registry.get_openai_tools.return_value = [MagicMock()]
    agent.tool_registry = mock_registry
    
    agent._resolve_prompt_for_variant = MagicMock(return_value=("prompt", "control", None))
    agent._prepare_llm_messages = AsyncMock(return_value=("sid", [], False))
    agent._add_message_to_session = AsyncMock()
    agent._publish_event = AsyncMock()
    agent._try_rule_fallback = AsyncMock(return_value=None)
    
    state = {
        "customer_query": "你好",
        "stream_callback": mock_cb,
        "session_id": "test-session",
    }
    
    result = await agent._process_with_tools(state, "你是一个客服")
    
    tool_events = [e for e in events if e["type"] in ("tool_call", "tool_result")]
    assert len(tool_events) == 0
    assert result["response"] == "直接回答"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_base_agent_streaming.py -v`
Expected: FAIL — old code doesn't emit tool_call/tool_result events

- [ ] **Step 3: Modify `_process_with_tools` — add thinking emit before loop**

In `agents/base_agent.py`, after `effective_llm = self._get_effective_llm(state)` (after line 520), add:

```python
        # v6.0: 全链路流式 — emit thinking + tool_call + tool_result 事件
        stream_callback = state.get("stream_callback")
        if stream_callback:
            try:
                await stream_callback({
                    "type": "thinking",
                    "content": f"🤔 {self.name} Agent 正在分析中...",
                    "agent": self.name,
                })
            except Exception:
                pass
```

- [ ] **Step 4: Modify `_process_with_tools` — emit tool_call and tool_result events**

After `parsed_tcs` is built (after line 552), add tool_call event emit:

```python
                # v6.0: emit tool_call events for visualization
                if stream_callback:
                    for p in parsed_tcs:
                        try:
                            await stream_callback({
                                "type": "tool_call",
                                "name": p["name"],
                                "args": p["args"],
                                "agent": self.name,
                            })
                        except Exception:
                            pass
```

After each tool result is obtained (after line 568), add tool_result event emit:

```python
                    # v6.0: emit tool_result event
                    if stream_callback:
                        try:
                            await stream_callback({
                                "type": "tool_result",
                                "name": p["name"],
                                "summary": result[:200] if result else "无结果",
                                "agent": self.name,
                            })
                        except Exception:
                            pass
```

- [ ] **Step 5: Modify final response — stream chunks instead of returning raw**

After the tool-calling loop completes and before self-reflection. When response_content is already set (non-tool-call path), and stream_callback exists, chunk the content:

After line 578 (`break`), the `else` branch already exited the loop. After the loop, pseudo-stream the final content if it came from a non-streamed path:

In the `else` branch (line 576-578), change from:
```python
            else:
                response_content = response.content
                break
```

To:
```python
            else:
                # v6.0: 有流式回调时使用 async_invoke_stream 输出最终响应
                if stream_callback:
                    try:
                        await stream_callback({
                            "type": "thinking",
                            "content": "💡 正在生成回答...",
                            "agent": self.name,
                        })
                    except Exception:
                        pass
                    response_content = ""
                    try:
                        async for chunk in effective_llm.async_invoke_stream(messages):
                            response_content += chunk
                            try:
                                await stream_callback({"type": "chunk", "content": chunk})
                            except Exception:
                                pass
                    except LLMServiceError as e:
                        is_quota = "Quota" in str(e)
                        self.logger.warning(
                            f"LLM 服务降级 (final) [{get_trace_id()}]: {e}",
                            exc_info=not is_quota,
                        )
                        response_content = (
                            "您的今日 Token 配额已用尽，请明日再试。" if is_quota else fallback_response
                        )
                        try:
                            await stream_callback({"type": "chunk", "content": response_content})
                        except Exception:
                            pass
                    except Exception as e:
                        self.logger.error(
                            f"LLM 流式调用异常 (final) [{get_trace_id()}]: {e}", exc_info=True
                        )
                        response_content = fallback_response
                        try:
                            await stream_callback({"type": "chunk", "content": fallback_response})
                        except Exception:
                            pass
                else:
                    response_content = response.content
                break
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/unit/test_base_agent_streaming.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add agents/base_agent.py tests/unit/test_base_agent_streaming.py
git commit -m "feat(agent): _process_with_tools emit thinking/tool_call/tool_result events"
```

---

### Task 4: RAG Status Events via ToolRegistry

**Files:**
- Modify: `tools/tool_registry.py` — `execute()` forward stream_callback to handler
- Modify: `agents/base_agent.py` — pass stream_callback to tool_registry.execute()

- [ ] **Step 1: Write test for RAG status stream_callback injection**

```python
# tests/unit/test_tool_registry_streaming.py
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.unit
async def test_tool_execute_forwards_stream_callback():
    """ToolRegistry.execute 将 stream_callback 转发给 tool handler"""
    from tools.tool_registry import ToolRegistry
    
    registry = ToolRegistry()
    handler_events = []
    
    async def test_handler(args, stream_callback=None):
        if stream_callback:
            await stream_callback({"type": "rag_status", "status": "retrieving"})
            handler_events.append(("called", args))
            await stream_callback({"type": "rag_status", "status": "done", "count": 3})
        return "result"
    
    registry.register("test_tool", "Test", {"type": "object", "properties": {}}, test_handler)
    
    cb_events = []
    async def mock_cb(e):
        cb_events.append(e)
    
    result = await registry.execute("test_tool", {"query": "test"}, stream_callback=mock_cb)
    assert result == "result"
    assert len(cb_events) == 2
    assert cb_events[0] == {"type": "rag_status", "status": "retrieving"}
    assert cb_events[1] == {"type": "rag_status", "status": "done", "count": 3}


@pytest.mark.unit
async def test_tool_execute_no_stream_callback():
    """无 stream_callback 时向后兼容"""
    from tools.tool_registry import ToolRegistry
    
    registry = ToolRegistry()
    
    async def handler(args):
        return "normal result"
    
    registry.register("t", "Test", {"type": "object", "properties": {}}, handler)
    result = await registry.execute("t", {})
    assert result == "normal result"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tool_registry_streaming.py -v`
Expected: FAIL — `ToolRegistry.execute()` doesn't accept `stream_callback` parameter

- [ ] **Step 3: Modify `ToolRegistry.execute` to accept and forward stream_callback**

In `tools/tool_registry.py`, modify the `execute` method (around line 60):

```python
    async def execute(
        self, name: str, arguments: dict[str, Any],
        stream_callback: Callable | None = None,  # v6.0: 转发给工具 handler
    ) -> str:
        """执行指定工具，返回字符串结果"""
        tool = self._tools.get(name)
        if not tool:
            return f"错误：工具 '{name}' 不存在"
        try:
            # v6.0: 注入 stream_callback，仅当 handler 接受此参数时传递
            if stream_callback is not None:
                import inspect
                sig = inspect.signature(tool.handler)
                if "stream_callback" in sig.parameters:
                    result = await tool.handler(arguments, stream_callback=stream_callback)
                else:
                    result = await tool.handler(arguments)
            else:
                result = await tool.handler(arguments)
            return str(result) if result is not None else "查询完成，无结果"
        except Exception as e:
            logger.error(f"工具执行失败 [{name}]: {e}", exc_info=True)
            return f"工具 '{name}' 执行失败，请稍后重试"
```

- [ ] **Step 4: Modify `_process_with_tools` to pass stream_callback to tool_registry.execute**

In `agents/base_agent.py`, change the tool execution line (around line 568):

```python
# OLD:
result = await self.tool_registry.execute(p["name"], p["args"])

# NEW:
result = await self.tool_registry.execute(
    p["name"], p["args"],
    stream_callback=state.get("stream_callback"),
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_tool_registry_streaming.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tools/tool_registry.py agents/base_agent.py tests/unit/test_tool_registry_streaming.py
git commit -m "feat(tools): ToolRegistry forwards stream_callback to handler"
```

---

### Task 5: Frontend SSE Event Handler Expansion

**Files:**
- Modify: `web/src/api/sse.js` — dispatch new event types to callbacks
- Modify: `web/src/chat/input.js` — wire new callbacks (onThinking, onToolCall, onRagStatus, onAgentSwitch)

- [ ] **Step 1: Expand SSE event dispatch in `sse.js`**

In `web/src/api/sse.js`, modify `sendChatStream()` function:

Change the event dispatch section (around lines 49-55):

```javascript
// OLD:
if (data.type === 'chunk' && onChunk) onChunk(data.content);
else if (data.type === 'done' && onDone) onDone(data);
else if (data.type === 'error' && onError) onError(data.content);
else if ((data.type === 'status' || data.type === 'progress') && onStatus)
  onStatus(data);

// NEW:
if (data.type === 'chunk' && onChunk) onChunk(data.content);
else if (data.type === 'done' && onDone) onDone(data);
else if (data.type === 'error' && onError) onError(data.content);
else if ((data.type === 'status' || data.type === 'progress') && onStatus)
  onStatus(data);
else if (data.type === 'thinking' && callbacks.onThinking)
  callbacks.onThinking(data);
else if (data.type === 'tool_call' && callbacks.onToolCall)
  callbacks.onToolCall(data);
else if (data.type === 'tool_result' && callbacks.onToolResult)
  callbacks.onToolResult(data);
else if (data.type === 'rag_status' && callbacks.onRagStatus)
  callbacks.onRagStatus(data);
else if (data.type === 'agent_switch' && callbacks.onAgentSwitch)
  callbacks.onAgentSwitch(data);
```

Make the same change in `sendChatStreamWithImage()` function (lines 124-129 area — same dispatch logic).

- [ ] **Step 2: Wire new callbacks in `input.js`**

In `web/src/chat/input.js`, modify `_sendViaSSE()` to pass new callbacks:

After the `const _controller = API.sendChatStream(query, getSessionId(), getSessionToken(), {` line (around line 109), add new callbacks:

```javascript
    onChunk(content) { /* unchanged */ },
    onDone(data) { /* unchanged */ },
    onError(errMsg) { /* unchanged */ },
    onStatus(_data) { /* unchanged */ },
    onThinking(data) {
      if (!hasStarted) {
        hasStarted = true;
        removeTypingIndicator();
        streaming = createStreamingMessage();
      }
      if (streaming) streaming.appendThinking(data.content);
    },
    onToolCall(data) {
      if (streaming) streaming.appendToolCall(data);
    },
    onToolResult(data) {
      if (streaming) streaming.appendToolResult(data);
    },
    onRagStatus(data) {
      if (streaming) streaming.updateRagStatus(data);
    },
    onAgentSwitch(data) {
      if (streaming) streaming.updateAgentTag(data.to);
    },
```

Make the same changes in `_sendViaSSEWithImage()`.

- [ ] **Step 3: Commit**

```bash
git add web/src/api/sse.js web/src/chat/input.js
git commit -m "feat(frontend): dispatch new SSE event types to callbacks"
```

---

### Task 6: Frontend Message Rendering — Thinking / Tool Call / RAG Status

**Files:**
- Modify: `web/src/chat/messages.js` — add appendThinking, appendToolCall, appendToolResult, updateRagStatus, updateAgentTag methods

- [ ] **Step 1: Add thinking rendering to `createStreamingMessage` return object**

In `web/src/chat/messages.js`, extend the `createStreamingMessage()` return object (after `finalize`, around line 268):

Add a `thinkingContainer` element in the message construction. In the wrapper creation (around lines 184-215), insert a thinking container after the bubble:

```javascript
createElement('div', { className: 'message-bubble' }, [
  createElement('span', { className: 'streaming-text' }),
  createElement('span', { className: 'streaming-cursor' }, ['▊']),
]),
createElement('div', { className: 'thinking-container', style: 'display:none' }),
```

Then extend the return object with new methods:

```javascript
return {
  appendChunk(chunk) { /* existing */ },
  finalize(meta = {}) { /* existing */ },
  
  // v6.0: 追加思考过程
  appendThinking(content) {
    const tc = wrapper.querySelector('.thinking-container');
    if (!tc) return;
    tc.style.display = 'block';
    const line = createElement('div', { className: 'thinking-line' }, [content]);
    tc.appendChild(line);
    scrollToBottom(container);
  },
  
  // v6.0: 追加工具调用卡片
  appendToolCall(data) {
    const tc = wrapper.querySelector('.thinking-container');
    if (!tc) return;
    tc.style.display = 'block';
    const card = createElement('div', { className: 'tool-call-card' }, [
      createElement('div', { className: 'tool-call-header' }, [
        createElement('span', { className: 'tool-call-icon' }, ['🛠️']),
        createElement('span', { className: 'tool-call-name' }, [data.name]),
      ]),
      createElement('pre', { className: 'tool-call-args' }, [JSON.stringify(data.args, null, 2)]),
    ]);
    tc.appendChild(card);
    scrollToBottom(container);
  },
  
  // v6.0: 追加工具结果卡片
  appendToolResult(data) {
    const tc = wrapper.querySelector('.thinking-container');
    if (!tc) return;
    const card = createElement('div', { className: 'tool-result-card' }, [
      createElement('div', { className: 'tool-result-header' }, [
        createElement('span', { className: 'tool-result-icon' }, ['📋']),
        createElement('span', { className: 'tool-result-name' }, [data.name]),
      ]),
      createElement('div', { className: 'tool-result-summary' }, [data.summary]),
    ]);
    tc.appendChild(card);
    scrollToBottom(container);
  },
  
  // v6.0: 更新 RAG 检索状态
  updateRagStatus(data) {
    const tc = wrapper.querySelector('.thinking-container');
    if (!tc) return;
    tc.style.display = 'block';
    let existing = tc.querySelector(`.rag-status-${data.status}`);
    if (!existing) {
      existing = createElement('div', { className: `rag-status rag-status-${data.status}` }, []);
      tc.appendChild(existing);
    }
    const statusTexts = {
      rewriting: '✏️ 查询改写中...',
      retrieving: '🔍 检索知识库中...',
      reranking: `📊 重排中 (${data.count || ''}条)`,
      done: `✅ 检索完成 (${data.count || 0}条结果)`,
    };
    existing.textContent = statusTexts[data.status] || data.status;
    scrollToBottom(container);
  },
  
  // v6.0: 更新 Agent 标签
  updateAgentTag(agentName) {
    const tag = wrapper.querySelector('.message-agent-tag');
    if (!tag) return;
    const icon = getAgentIcon(agentName);
    tag.replaceChildren(
      createElement('span', { className: 'agent-icon' }, [icon]),
      ` ${getAgentDisplayName(agentName)}`,
    );
  },
};
```

- [ ] **Step 2: Add CSS for new streaming elements**

In `web/src/css/` (check existing files), add styles for the new elements:

```css
/* 思考过程容器 */
.thinking-container {
  background: var(--bg-secondary, #f5f5f5);
  border-radius: 8px;
  padding: 8px 12px;
  margin-top: 8px;
  font-size: 0.9em;
}

.thinking-line {
  color: var(--text-secondary, #666);
  font-style: italic;
  padding: 4px 0;
}

/* 工具调用卡片 */
.tool-call-card {
  background: var(--bg-code, #1e1e1e);
  border: 1px solid var(--border-color, #ddd);
  border-radius: 6px;
  margin: 6px 0;
  overflow: hidden;
}

.tool-call-header {
  background: var(--bg-hover, #eee);
  padding: 4px 8px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.tool-call-name {
  font-weight: 600;
  font-size: 0.9em;
}

.tool-call-args {
  padding: 8px;
  margin: 0;
  font-size: 0.8em;
  overflow-x: auto;
  white-space: pre-wrap;
}

/* 工具结果卡片 */
.tool-result-card {
  background: var(--bg-secondary, #f9f9f9);
  border-left: 3px solid var(--accent-color, #4a9eff);
  border-radius: 4px;
  margin: 6px 0;
  padding: 6px 10px;
}

.tool-result-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}

.tool-result-summary {
  font-size: 0.85em;
  color: var(--text-secondary, #555);
}

/* RAG 状态指示器 */
.rag-status {
  padding: 3px 0;
  font-size: 0.85em;
  color: var(--text-secondary, #666);
}
```

- [ ] **Step 3: Commit**

```bash
git add web/src/chat/messages.js web/src/css/streaming-events.css
git commit -m "feat(frontend): render thinking/tool_call/rag_status streaming events"
```

---

### Task 7: Tests and Verification

**Files:**
- Test: `tests/unit/test_graph_builder.py` — existing tests for graph nodes
- Test: `tests/unit/test_base_agent_streaming.py` — existing tests for process_with_tools
- Test: `tests/unit/test_tool_registry_streaming.py` — existing tests

- [ ] **Step 1: Run all tests**

Run: `make test-fast` or `pytest tests/unit/ -x -v`
Expected: ≥80% pass, any failures are real issues to fix

- [ ] **Step 2: Verify SSE generator handles all event types**

Write an integration test for `_sse_stream_generator`:

```python
# tests/unit/test_sse_streaming.py
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.unit
async def test_sse_stream_generator_forwards_event_types():
    """SSE generator 转发所有支持的 event types"""
    from api.routes.chat import _sse_event, _sse_stream_generator, SSEStreamContext
    
    queue = AsyncMock()
    queue.get = AsyncMock(side_effect=[
        {"type": "status", "phase": "cache", "content": "test"},
        {"type": "thinking", "content": "thinking...", "agent": "test"},
        {"type": "tool_call", "name": "search_kb", "args": {}, "agent": "test"},
        {"type": "tool_result", "name": "search_kb", "summary": "results", "agent": "test"},
        {"type": "rag_status", "status": "done", "count": 3},
        {"type": "agent_switch", "from": "router", "to": "product"},
        None,  # termination signal
    ])
    
    graph_task = AsyncMock()
    graph_task.done = MagicMock(return_value=False)
    
    ctx = SSEStreamContext(
        graph_task=graph_task,
        chunk_queue=queue,
        sid="test",
        session_manager=None,
        client_provided_sid=False,
        status_msg="Processing...",
        progress_msg="",
    )
    
    events = []
    async for event in _sse_stream_generator(ctx):
        events.append(event)
    
    # Only the 6 non-None dict events should be forwarded
    # (status and progress are initial, then the 7 from queue, then done)
    yielded_types = []
    for e in events:
        import json
        data = json.loads(e.removeprefix("data: "))
        yielded_types.append(data.get("type"))
    
    assert "status" in yielded_types
    assert "thinking" in yielded_types
    assert "tool_call" in yielded_types
    assert "tool_result" in yielded_types
    assert "rag_status" in yielded_types
    assert "agent_switch" in yielded_types
```

- [ ] **Step 3: Run integration test**

Run: `pytest tests/unit/test_sse_streaming.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_sse_streaming.py
git commit -m "test: SSE streaming event types integration tests"
```

---

### Summary of Files Changed

| File | Change |
|---|---|
| `core/graph_builder.py` | Add `_emit_status()` helper + inject into `_check_cache_node`, `_classify_query_node`, `_execute_collaboration` |
| `api/routes/chat.py` | Forward all event types in `_sse_stream_generator` |
| `agents/base_agent.py` | `_process_with_tools()` emit thinking/tool_call/tool_result + stream final response |
| `tools/tool_registry.py` | `execute()` accepts and forwards `stream_callback` |
| `web/src/api/sse.js` | Dispatch new event types to callbacks |
| `web/src/chat/input.js` | Wire onThinking/onToolCall/onRagStatus/onAgentSwitch |
| `web/src/chat/messages.js` | Add appendThinking/appendToolCall/appendToolResult/updateRagStatus |
| `web/src/css/*.css` | Styles for thinking container, tool cards, RAG status |
| `tests/unit/test_graph_builder.py` | Tests for `_emit_status` |
| `tests/unit/test_base_agent_streaming.py` | Tests for `_process_with_tools` streaming events |
| `tests/unit/test_tool_registry_streaming.py` | Tests for `ToolRegistry.execute` callback forwarding |
| `tests/unit/test_sse_streaming.py` | Tests for SSE generator event type forwarding |
