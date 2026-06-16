# 角色审计报告 — 可观测性督察（Observability Inspector）

> **审计日期**: 2026-06-15
> **审计范围**: core/monitoring.py、alerts/、api/middleware.py、core/logger.py、docker-compose.monitoring.yml
> **发现总数**: 15 个
> **总体评分**: 5.8/10

---

## 发现列表

| 编号 | 严重 | 问题 | 位置 | 影响 | 修复建议 |
|------|------|------|------|------|----------|
| **B-1** | 🔴 P0 | Prometheus 指标名不匹配 | `core/monitoring.py` | 告警规则中的指标名（如 `csai_error_rate_percent`）与实际代码输出不匹配，告警永远不会触发 | 统一指标名 |
| **B-2** | 🔴 P0 | 告警规则指标名不匹配 | `alerts/` | 告警规则引用不存在的指标名 | 修复规则 |
| **B-3** | 🔴 P0 | 仅 5 处 `exc_info=True` | 全局 | 大量错误日志缺少堆栈追踪，故障排查困难 | 增加堆栈追踪 |
| B-4 | 🟠 P1 | 日志未对敏感信息（API Key、密码、Token）脱敏 | `core/logger.py` | 敏感信息泄露风险 | 敏感字段脱敏 |
| B-5 | 🟠 P1 | OpenTelemetry 默认未启用，分布式追踪完全缺失 | 全局 | 无法追踪跨服务调用 | 配置 OTel |
| B-6 | 🟠 P1 | `trace_id` 仅 12 字符（`uuid[:12]`），非标准格式，碰撞风险 | `core/logger.py` | trace_id 碰撞 | 使用标准 UUID |
| B-7 | 🟠 P1 | 缺少 Histogram 类型指标，无法计算 P99/P99.9 分位数 | `core/monitoring.py` | 无法做性能分位分析 | 添加 Histogram |
| B-8 | 🟠 P1 | Alertmanager webhook URL 指向不存在的端点 `/api/alerts/webhook` | `alerts/` | 告警无法送达 | 修复端点 |
| B-9 | 🟡 P2 | 日志级别分布失衡（info+warning 占 67%） | 全局 | 关键错误被淹没 | 调整日志级别 |
| B-10 | 🟡 P2 | user_id 未注入日志 | `core/logger.py` | 无法追踪用户级问题 | 注入 user_id |
| B-11 | 🟡 P2 | 无 runbook / 故障处理手册 | 全局 | 事故响应耗时 | 编写 runbook |
| B-12 | 🟡 P2 | docker-compose.monitoring.yml 缺少 Prometheus/Alertmanager 服务 | `docker-compose.monitoring.yml` | 监控栈不完整 | 补充服务 |
| B-13 | 🟡 P2 | Loki 保留仅 30 天 | `docker-compose.monitoring.yml` | 日志保留不足 | 延长保留期 |
| B-14 | 🟢 P3 | JSON 序列化容错 | `core/logger.py` | 日志序列化失败 | 添加容错 |
| B-15 | 🟢 P3 | Alertmanager repeat_interval 过长 | `alerts/` | 告警重复间隔过长 | 调整间隔 |

---

## 详细分析

### B-1: Prometheus 指标名不匹配 (P0)

**位置**: `core/monitoring.py`

**问题**: 代码中定义的指标名与告警规则中的指标名不一致。

**影响**: 告警规则永远不会触发，因为指标名不匹配。

**修复建议**: 统一指标命名规范，确保代码和告警规则一致。

### B-2: 告警规则指标名不匹配 (P0)

**位置**: `alerts/`

**问题**: 告警规则引用不存在的指标名。

**影响**: 告警永远不会触发。

**修复建议**: 修复告警规则中的指标名。

### B-3: 仅 5 处 `exc_info=True` (P0)

**位置**: 全局

**问题**: 大量错误日志缺少堆栈追踪。

**影响**: 故障排查困难，无法定位问题根因。

**修复建议**: 在关键错误日志中增加 `exc_info=True`。

---

## 总结

| 严重程度 | 数量 |
|----------|------|
| 🔴 P0 (Critical) | 3 |
| 🟠 P1 (High) | 5 |
| 🟡 P2 (Medium) | 5 |
| 🟢 P3 (Low) | 2 |
| **总计** | **15** |

**优先修复顺序**: B-1 → B-2 → B-3 → B-4 → B-5 → B-6 → B-7 → B-8 → 其余
