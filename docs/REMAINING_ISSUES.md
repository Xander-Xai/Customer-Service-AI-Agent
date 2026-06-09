# 代码质量改进 — 当前遗留问题清单

> 生成时间：2026-06-08
> 阶段：Phase 1-5 全部完成后的遗留问题

---

## 1. 测试覆盖未达标（72% → 目标 80%）

**当前状态**：6201 语句，1750 未覆盖，覆盖率 72%
**差距**：需再覆盖约 510 个语句才能达到 80%

### 未覆盖模块（按缺失语句数排序）

| 模块 | 总语句 | 未覆盖 | 当前覆盖 | 优先级 |
|------|--------|--------|---------|--------|
| `api/app_factory.py` | 71 | 71 | 0% | 高 |
| `session_manager.py` | 343 | 253 | 26% | 高 |
| `api/routes/chat.py` | 348 | 97 | 72% | 中 |
| `agents/base_agent.py` | 290 | 118 | 59% | 中 |
| `agents/evaluator.py` | 234 | 74 | 68% | 中 |
| `llm/client.py` | 192 | 96 | 50% | 中 |
| `api/middleware.py` | 188 | 60 | 68% | 中 |
| `api/app.py` | 150 | 50 | 67% | 低 |
| `rag/knowledge_base.py` | 223 | 121 | 46% | 中 |
| `collaboration/modes.py` | 215 | 40 | 81% | 低 |
| `alembic/env.py` + versions | 91 | 91 | 0% | 低（迁移脚本） |
| `erp/kingdee_real_adapter.py` | 220 | 31 | 86% | 低 |
| `core/monitoring.py` | 220 | 57 | 74% | 低 |
| `api/routes/ws.py` | 165 | 131 | 21% | 中 |
| `core/container.py` | 88 | 51 | 42% | 低 |
| `drift_detector.py` | 150 | 2 | 99% | — |

### 覆盖率提升策略

1. **api/app_factory.py (0%→80%)**：71语句，添加应用工厂启动测试（约需30行测试）
2. **session_manager.py (26%→75%)**：253未覆盖，添加会话CRUD、消息管理、滑动窗口测试（约需200行测试，最大工作量）
3. **api/routes/ws.py (21%→75%)**：131未覆盖，WebSocket连接/消息/超时测试（约需100行）
4. **llm/client.py (50%→75%)**：96未覆盖，重试/熔断/降级路径测试（约需80行）
5. **rag/knowledge_base.py (46%→75%)**：121未覆盖，ChromaDB操作mock测试（约需100行）
6. **api/middleware.py (68%→80%)**：60未覆盖，限流/安全头/CSRF测试（约需50行）

---

## 2. Ruff Lint 残留问题（73 个）

**当前状态**：346→73（已修复 273 个）

| 类型 | 数量 | 说明 | 处理建议 |
|------|------|------|---------|
| SIM117 | 39 | 多个with语句可合并 | 代码风格，可自动修复（`--fix --unsafe-fixes`） |
| E402 | 16 | 模块导入不在文件顶部 | 有意为之（延迟导入/条件导入），添加 `noqa` |
| SIM102 | 6 | if可合并 | 代码风格，手动评估 |
| B017 | 4 | pytest.raises(Exception)过宽 | 改为具体异常类型 |
| SIM105 | 3 | 可用contextlib.suppress | 代码风格，手动评估 |
| B007 | 2 | 循环变量未使用 | 改为 `_` |
| B905 | 1 | zip缺少strict参数 | 添加 `strict=False` |
| E712 | 1 | `== True` 比较 | 改为 `is True` |
| E741 | 1 | 变量名歧义（如 `l`） | 重命名 |

### 处理建议

- **SIM117 (39个)**：运行 `ruff check . --fix --unsafe-fixes --select SIM117` 自动修复
- **E402 (16个)**：在每行添加 `# noqa: E402` 注释
- **其余 (18个)**：手动逐一修复，风险低

---

## 3. 其他遗留问题

### 3.1 测试质量问题
- 2 个 OpenTelemetry 测试已修复隔离问题（添加 autouse fixture 重置全局状态）
- `test_no_hardcoded_secrets` 可能因测试数据中的字符串触发误报

### 3.2 代码质量
- `session_manager.py` 仍为 670+ 行（评估后决定不拆分，已添加段落标记）
- `_dual_mode` 装饰器仍使用 `asyncio.run()`（已添加监控日志，保守方案）
- 100+ `except Exception` 已添加日志记录，但部分仍保留宽泛捕获

### 3.3 安全
- PBKDF2→Argon2id 升级仅添加注释说明（未实际改动）
- CSRF 防护在 DEV_MODE 下跳过（生产环境生效）

### 3.4 依赖管理
- `opencv-python-headless`、`edge-tts`、`pdfplumber`、`python-docx` 已改为延迟导入，但仍为必选依赖（未移至 requirements-optional.txt）
- 传递依赖冲突（python-jose/passlib）已标注但未实际移除

---

## 4. 建议的下一步行动

| 优先级 | 任务 | 预估工作量 | 预期收益 |
|--------|------|-----------|---------|
| P0 | 补充 session_manager 测试（253语句） | 1-2小时 | 覆盖率 +4% |
| P0 | 补充 api/app_factory 测试（71语句） | 30分钟 | 覆盖率 +1% |
| P1 | 补充 llm/client + rag/knowledge_base 测试 | 1-2小时 | 覆盖率 +3.5% |
| P1 | 补充 api/routes/ws 测试（131语句） | 1小时 | 覆盖率 +2% |
| P2 | ruff SIM117 自动修复 (39个) | 5分钟 | lint 问题 -39 |
| P2 | E402 添加 noqa 注释 (16个) | 10分钟 | lint 问题 -16 |
| P3 | 手动修复剩余 ruff 问题 (18个) | 30分钟 | lint 清零 |
| P3 | 创建 requirements-optional.txt | 15分钟 | 依赖治理 |
