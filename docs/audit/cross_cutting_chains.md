# 横切审计链报告

> **生成日期**: 2026-06-15
> **审计范围**: 数据流、故障传播、配置安全
> **依据**: 05-多角色并行审计模板.md v2.0 Phase 2.5

---

## 横切链 1：数据流审计（Data Flow Trail）

```
输入 → 验证 → 转换 → 存储 → 读取 → 展示 → 导出 → 删除
```

| 节点 | 状态 | 涉及角色 | 问题 |
|------|------|---------|------|
| **输入** | ⚠️ | A, G | sanitize_input 已做，但 MAX_QUERY_LENGTH 环境变量未在运行时校验（A-5） |
| **验证** | ⚠️ | A, G | Pydantic 验证 + JWT 鉴权，但 DEV_MODE 完全绕过（A-8） |
| **转换** | ✅ | C, G | 无精度丢失 |
| **存储** | ❌ | A, W | **明文存储**（H-2），无加密；Session 文件明文 JSON（S-1） |
| **读取** | ⚠️ | A, W | 缓存一致性待验证（C-4）；内存与持久化不一致 |
| **展示** | ⚠️ | A, U | XSS 防护有 DOMPurify，但 welcome.js data-title 未转义（A-7） |
| **导出** | ✅ | A, L | 无导出功能 |
| **删除** | ❌ | W, V | 无软删除，物理删除无法恢复（D-7） |

### 数据流审计发现

| 编号 | 前缀 | 严重 | 问题 | 位置 |
|------|------|------|------|------|
| DF-1 | 🔴 | P0 | Session 明文存储 | `core/session/session_manager.py:240` |
| DF-2 | 🟠 | P1 | PII 直发第三方 LLM | `agents/billing_agent.py:69` |
| DF-3 | 🟡 | P2 | 缓存一致性风险 | `core/session/session_manager.py:180` |
| DF-4 | 🟡 | P2 | 无软删除 | `db/models.py` |
| DF-5 | 🟡 | P2 | 输入长度校验不一致 | `api/routes/chat.py:34` |

---

## 横切链 2：故障传播审计（Failure Propagation Trail）

```
LLM API 挂了 → graph_builder 降级 → RuleBasedLLM → 前端展示什么？
```

| 层级 | 状态 | 涉及角色 | 问题 |
|------|------|---------|------|
| **LLM API** | ⚠️ | F, G | CircuitBreaker 已配置，但 user_id 传递链断裂（H-1） |
| **graph_builder** | ⚠️ | F, G | 降级到规则分类，但 Agent 级无 fallback（H-5） |
| **业务层** | ✅ | F, G | 有熔断器 + 降级响应 |
| **API 层** | ✅ | A, F | 统一错误格式（但 U-1 错误键不统一） |
| **前端层** | ❌ | U, F | 401 无提示，空 catch（U-2） |
| **告警层** | ❌ | B, O | 告警规则指标名不匹配（B-1/B-2） |

### 故障传播审计发现

| 编号 | 前缀 | 严重 | 问题 | 位置 |
|------|------|------|------|------|
| FP-1 | 🔴 | P0 | user_id 传递链断裂 | `llm/client.py:158` |
| FP-2 | 🔴 | P0 | 告警规则失效 | `alerts/` |
| FP-3 | 🟠 | P1 | Agent 级无 fallback | `agents/base_agent.py:638` |
| FP-4 | 🟡 | P2 | 错误键不统一 | `api/routes/chat.py:207` |
| FP-5 | 🟡 | P2 | Token 过期无提示 | `web/src/chat/sessions.js:73` |

---

## 横切链 3：配置安全审计（Configuration Security Trail）

```
.env.example → .env.local → .env.staging → .env.production → Docker env → K8s ConfigMap/Secret
```

| 节点 | 状态 | 涉及角色 | 问题 |
|------|------|---------|------|
| **.env.example** | ✅ | S, C | 占位符，无真实值 |
| **.env.dev** | ⚠️ | S, A | DEV_MODE=true，完全绕过认证（A-8） |
| **.env.prod** | ✅ | S, A | 未提交到 git |
| **Docker** | ⚠️ | S, O | 未验证是否以非 root 运行 |
| **K8s** | N/A | S, O | 未使用 K8s |
| **Feature Flag** | ❌ | O, M | 无 Feature Flag 机制 |

### 配置安全审计发现

| 编号 | 前缀 | 严重 | 问题 | 位置 |
|------|------|------|------|------|
| CS-1 | 🔴 | P0 | CSP nonce 格式错误 | `api/middleware.py:168` |
| CS-2 | 🔴 | P0 | WS 认证绕过 | `api/routes/ws.py:63` |
| CS-3 | 🟠 | P1 | 依赖 39 个已知漏洞 | `requirements-lock.txt` |
| CS-4 | 🟡 | P2 | DEV_MODE 完全绕过 | `api/middleware.py:241` |
| CS-5 | 🟡 | P2 | 无 Feature Flag | 全局 |

---

## 横切链汇总

| 横切链 | 前缀 | 追踪对象 | P0 数 | P1 数 | P2 数 | 总计 |
|--------|------|---------|-------|-------|-------|------|
| 数据流审计 | DF | 数据完整生命周期 | 1 | 1 | 3 | 5 |
| 故障传播审计 | FP | 故障级联路径 | 2 | 1 | 2 | 5 |
| 配置安全审计 | CS | 配置项全链路 | 2 | 1 | 2 | 5 |
| **总计** | — | — | **5** | **3** | **7** | **15** |
