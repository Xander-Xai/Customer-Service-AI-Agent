# 药妆智多星 v5.4.1 增量发布说明

**发布日期**: 2026-06-17
**版本类型**: 补丁版（Patch Release）
**主题**: 前后端 API 对齐 + 前端模块清理

---

## 🎯 版本目标

完整审计后端全部 REST / SSE / WebSocket 端点并与前端实现对齐，消除隐藏的死代码与功能盲点。

---

## ✅ 已完成项

### 1. 前端 API 覆盖度审计
- 扫描后端 `api/`、`auth/`、`knowledge/`、`alerts/` 全部 47 个端点
- 交叉对比 `web/src/api/rest.js` 已实现方法
- 输出 8×5 覆盖度矩阵（5 个类别 × N 个端点）

### 2. 补齐前端缺失 REST API（5 个端点）
| 后端端点 | 前端封装 | 状态 |
|---|---|---|
| `GET /api/sessions/{id}/checkpoint` | `getSessionCheckpoint` | ✅ 新增 |
| `GET /api/history` | `getHistory` | ✅ 新增 |
| `GET /api/history/{id}/messages` | `getHistoryMessages` | ✅ 新增 |
| `GET /api/monitoring/token-quota` | `getTokenQuota` | ✅ 新增 |
| `GET /metrics/prometheus` | `getPrometheusMetrics` | ✅ 新增 |

所有新方法都通过 `fetchWithAuth` 中央认证拦截器（自动 401 刷新 + 重试）。

### 3. 新增前端功能模块
- `web/src/admin-token-quota.js` — 监控概览新增"我的 Token Quota"卡片
  - 今日/本月使用率 + 限额对比
  - 90%/70% 阈值颜色告警（红/黄/绿）
  - 按角色加载：admin/supervisor/agent/customer 均能查看自己的配额
- `web/src/admin-history.js` — 历史会话/消息查询工具
  - 列表加载 + 单会话消息渲染
  - 供管理后台/审计视图复用

### 4. 管理后台 UI 增强
- `admin.html` 监控概览板块末尾新增 `tokenQuotaCard` 卡片
- `admin.js` 角色加载逻辑扩展：所有角色都加载 `loadTokenQuota()`
- `chat/sessions.js` 选择会话时静默触发 checkpoint 探测（断点续传就绪标记）

### 5. 死代码清理
- 删除 `web/src/admin-analytics-core.js`（274 行，与 `admin-analytics.js` 重复）
- 删除 `web/src/admin-analytics-charts.js`（213 行，无引用）
- **净减 487 行冗余代码**，无任何调用方受影响

---

## 📊 改进指标

| 维度 | 数值 |
|---|---|
| 后端 REST 端点覆盖率 | 100% (47/47) |
| 新增前端 API 方法 | 5 个 |
| 新增前端模块 | 2 个 |
| 删除冗余代码 | 487 行 |
| 前端测试 | 53/53 通过 |
| Vite 构建 | 52 模块，~190ms |
| Biome lint | 0 error（仅 8 个 CSS 警告，与本版本无关） |
| 后端测试套件 | 1352 tests collected |

---

## 🔧 技术细节

### fetchWithAuth 中央拦截器复用
所有新增 API 复用 `web/src/auth/index.js` 中的 `fetchWithAuth()`：
- 自动注入 `Authorization: Bearer <token>`
- 401 响应自动尝试 refresh_token 刷新并重试一次
- 二次 401 触发 `auth:expired` 事件 + 自动跳登录

### 与状态管理集成
- `getSessionCheckpoint()` 调用不阻塞 UI，错误静默（断点续传是优化项而非必需）
- 命中 checkpoint 时写入 `sessionStorage` 标记（`cp:<session_id>=1`），供后续图执行参考

### Token Quota 渐进展示
- admin/supervisor：监控概览首屏可见
- agent/customer：仅展示自己用量
- API 401 时降级为"请登录"toast，不污染主界面

---

## 🚀 后续可拓展项

- 监控 Prometheus 指标的 Grafana dashboard JSON 模板
- Token Quota 超限时的主动警告横幅（当前仅颜色提示）
- 历史消息视图嵌入管理后台（已有 `renderHistoryMessages` 函数，等待 UI 集成）

---

## 📁 受影响文件

```
web/src/api/rest.js            | +35 行 (5 个新 API)
web/src/api/index.js           | +8 行  (新方法暴露)
web/src/admin-token-quota.js   | +94 行 (新模块)
web/src/admin-history.js       | +71 行 (新模块)
web/src/admin.js               | +9 行  (集成 loadTokenQuota)
web/src/chat/sessions.js       | +14 行 (checkpoint 探测)
web/admin.html                 | +7 行  (tokenQuotaCard)
web/src/admin-analytics-core.js   | -274 行 (删除)
web/src/admin-analytics-charts.js | -213 行 (删除)
```

**净增/减**: +238 行 / -487 行 = **净减 249 行**
