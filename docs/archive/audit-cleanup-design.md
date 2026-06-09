# 代码质量安全审查 + 冗余清理设计文档

**日期：** 2026-06-08
**范围：** 全项目 Python + 前端代码审查，冗余清理，安全加固

---

## 1. 目标

1. **冗余清理**：删除已被新前端替代的旧代码（~4130 行），清理 worktree 快照
2. **职责修正**：将 LLM 客户端从 `core/monitoring.py` 迁移到 `llm/` 模块
3. **安全审查**：Agent 自动扫描全项目，发现并修复安全漏洞
4. **质量审查**：Agent 自动扫描，发现重复逻辑、死代码、复杂度过高等问题
5. **验证**：清理和修复后，全量测试通过

---

## 2. Phase 1: 冗余清理

### 2.1 删除旧前端文件

**删除文件清单（~4130 行）：**

| 文件 | 行数 | 说明 |
|---|---|---|
| `templates/index.html` | 304 | 已被根目录 index.html + Vite 替代 |
| `templates/admin.html` | 290 | 已被根目录 admin.html + Vite 替代 |
| `templates/login.html` | 145 | 已被根目录 login.html + Vite 替代 |
| `static/css/style.css` | 1726 | 已被 styles/ 8 个文件替代 |
| `static/js/chat.js` | 1220 | 已被 src/chat/ 7 个模块替代 |
| `static/js/api.js` | 445 | 已被 src/api/ 5 个模块替代 |
| `static/js/admin.js` | 175 | 已被 src/admin.js 替代 |

**删除目录：**
- `templates/`（整个目录）
- `static/js/`（整个目录）
- `static/css/`（整个目录）

**保留：**
- `static/dist/`（Vite 构建产物，由 FastAPI 服务）
- `static/` 目录本身（可能有其他用途如上传文件）

### 2.2 清理 app.py 回退逻辑

当前 `api/app.py` 中的 `_html_path()` 函数：

```python
def _html_path(filename: str) -> str:
    dist_path = os.path.join(dist_dir, filename)
    if os.path.exists(dist_path):
        return dist_path
    return os.path.join(templates_dir, filename)  # 回退到 templates/
```

**改为：**

```python
def _html_path(filename: str) -> str:
    return os.path.join(dist_dir, filename)
```

同时移除 `templates_dir` 变量定义。

### 2.3 清理 .claude/worktrees/

删除 `.claude/worktrees/agent-a2a51c236f26cf12c/` 和 `.claude/worktrees/agent-a3a22480678679df5/`（旧代码快照，非 git worktree）。

### 2.4 修正 core/monitoring.py 职责

**迁移内容：**

从 `core/monitoring.py` 中提取以下两个类到 `llm/client.py`：
- `CustomResponse`（~20 行）— LLM 响应封装
- `OpenAICompatibleClient`（~140 行）— LLM HTTP 客户端

**更新 import 引用：**

| 文件 | 旧 import | 新 import |
|---|---|---|
| `core/container.py` | `from core.monitoring import OpenAICompatibleClient` | `from llm.client import OpenAICompatibleClient` |
| `agents/evaluator.py` | `from core.monitoring import OpenAICompatibleClient` | `from llm.client import OpenAICompatibleClient` |

**更新 `llm/__init__.py`：**

```python
from llm.client import OpenAICompatibleClient, CustomResponse
from llm.rule_based_llm import RuleBasedLLM
```

**清理 `core/monitoring.py`：**
- 移除迁出的两个类
- 移除 `langchain_core.messages` 的未使用导入（如果迁移到 llm/client.py 后不需要）

### 2.5 移除未使用导入

使用 Agent 扫描全项目 Python 文件，识别并移除未使用的 import 语句。

---

## 3. Phase 2: Agent 审查

### 3.1 并行审查策略

启动两个独立 Agent 并行工作：

**Agent 1: security-auditor**

| 审查维度 | 检查内容 |
|---|---|
| 输入校验 | API 端点的参数验证、类型检查、长度限制 |
| 认证授权 | JWT 处理、API Key 验证、权限检查覆盖完整性 |
| 注入防护 | SQL 注入（SQLAlchemy 使用）、XSS（HTML 输出转义）、命令注入 |
| 敏感数据 | 密钥/密码处理、日志中是否泄露敏感信息、.env 文件安全 |
| 依赖安全 | requirements.txt 中的已知漏洞依赖 |
| WebSocket | 连接认证、消息验证、DoS 防护 |

**Agent 2: code-reviewer**

| 审查维度 | 检查内容 |
|---|---|
| 模块职责 | 是否有类/函数做了超出模块职责的事 |
| 重复逻辑 | 跨文件的重复代码模式 |
| 函数复杂度 | 过长函数（>100行）、过深嵌套、圈复杂度 |
| 错误处理 | 异常是否被正确捕获和处理，是否有裸 except |
| 异步安全 | async/await 使用是否正确，是否有竞态条件 |
| 类型注解 | 公共 API 是否有类型注解 |

### 3.2 审查范围

**Python（~17,174 行）：**
- `api/app.py`（1520 行）— 最大文件，重点审查
- `core/monitoring.py`（592 行）— 迁移后审查
- `session_manager.py`（583 行）— 核心业务逻辑
- `agents/base_agent.py`（533 行）— Agent 基类
- `rag/seed_data.py`（1391 行）— 大文件
- 其他所有 .py 文件

**前端（~4,568 行）：**
- `src/` 目录（22 个 JS 模块）
- 重点：`src/chat/input.js`（SSE 降级逻辑）、`src/api/websocket.js`（认证）

### 3.3 输出格式

审查结果统一汇总为：

```markdown
## 审查发现

### Critical（必须修复）
- [C1] 文件:行号 — 问题描述 — 修复建议

### High（强烈建议修复）
- [H1] ...

### Medium（建议修复）
- [M1] ...

### Low（可选修复）
- [L1] ...
```

---

## 4. Phase 3: 修复 + 验证

### 4.1 修复策略

- Critical/High：必须在本次修复
- Medium：本次修复（如果改动范围小）
- Low：记录到 TODO，后续处理

### 4.2 验证

- 每次修复后运行 `pytest tests/ -v -m "not stress and not e2e"`（快速回归测试）
- 全部修复完成后运行 `pytest tests/ -v`（完整测试）
- 确认 `npx vite build` 仍然成功

---

## 5. 执行顺序

```
Step 1: 删除旧前端文件 + 目录
Step 2: 清理 app.py 回退逻辑
Step 3: 清理 .claude/worktrees/
Step 4: 迁移 OpenAICompatibleClient 到 llm/client.py
Step 5: 运行测试确认 Phase 1 无回归
Step 6: 并行启动 security-auditor + code-reviewer Agent
Step 7: 汇总审查报告
Step 8: 按优先级逐项修复
Step 9: 全量测试 + Vite 构建验证
```
