# 面试项目问题清单与修复记录

> 生成时间：2026-06-05 | 基于全量代码审查

## 问题清单

| # | 问题 | 严重程度 | 面试风险 | 状态 |
|---|------|---------|---------|------|
| 1 | SSE 流式是伪流式（先等完整回复再分块推送） | 🔴 High | 被追问"真 streaming 还是假的"会尴尬 | ✅ 已修复 (v4.2) |
| 2 | 全局变量模式（模块级全局实例） | 🟡 Medium | 可说"正在迁移"，但两套代码共存不优雅 | ✅ 已修复 (v4.2) |
| 3 | ERP 是 Mock 的 | 🟢 Low | 接口抽象已做好，面试时解释即可 | ✅ 无需修复 |
| 4 | 前端 CSP 允许 unsafe-inline | 🟢 Low | 可说"下一步迁移到 DOMPurify" | ✅ 无需修复 |
| 5 | 测试全是 Mock | 🟢 Low | 测试逻辑正确性，LLM 效果需集成环境 | ✅ 无需修复 |

---

## 修复记录

### 问题 1：SSE 伪流式 → 真流式 ✅ 已修复

**现状**：`/api/chat/stream` 端点先调用 `_run_graph()` 获取完整回复，再按 20 字符分块推送，模拟流式效果。

**修复方案**：
- `OpenAICompatibleClient` 新增 `async_invoke_stream()` 方法，使用 `httpx.AsyncClient.stream()` + SSE 解析逐 token 接收
- `BaseAgent._process_with_llm()` 内置流式检测：当 `state["stream_callback"]` 存在时自动切换为真流式，所有 Agent 零改动获得流式能力
- `/api/chat/stream` 端点改为 asyncio.Queue 桥接模式：Agent 推 chunk → Queue → SSE Generator → 前端
- 新增 `_run_graph_stream()` 辅助函数，将 stream_callback 注入图状态

**改动文件**：
- `core/monitoring.py` — 新增 `async_invoke_stream()` 方法（~60 行）
- `agents/base_agent.py` — `_process_with_llm()` 自动检测流式 + 新增 `_process_with_llm_stream()` 备用方法
- `api/app.py` — `/api/chat/stream` 端点重写 + 新增 `_run_graph_stream()`
- `tests/test_modules.py` — 新增 `TestStreamingLLM` 3 个测试用例

**测试结果**：363 passed ✅

### 问题 2：全局变量 → ServiceContainer 完全迁移 ✅ 已修复

**现状**：`multi_agent_customer_service.py` 有大量模块级全局变量（llm, agents_dict, session_mgr 等），`core/container.py` 的 `ServiceContainer` 已实现但 `app_factory.py` 混合使用新旧两套方式。

**修复方案**：
- `api/app_factory.py` 完全重写为纯容器模式：
  - 移除 `from multi_agent_customer_service import make_graph, session_mgr, cache...` 导入
  - 使用 `ServiceContainer` 管理所有服务生命周期
  - Graph 在 lifespan 中异步构建（`_container.initialize()` → `_container.graph_app`）
  - 保留向后兼容：lifespan 中将容器服务注入到 `api/app.py` 的全局引用

**改动文件**：
- `api/app_factory.py` — 完全重写（~130 行）

**测试结果**：363 passed ✅

### 问题 3-5：无需代码修复

- **ERP Mock**：`erp/__init__.py` 的抽象接口 + `erp/factory.py` 的工厂模式 + `erp/kingdee_real_adapter.py` 的真实适配器已完备
- **CSP unsafe-inline**：前端使用内联事件处理器，迁移成本高且不影响面试评估
- **测试 Mock**：MockLLM 是 LLM 应用测试的标准做法，验证的是业务逻辑而非 LLM 效果
