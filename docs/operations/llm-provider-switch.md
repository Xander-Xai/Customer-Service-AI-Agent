# LLM 供应商快速切换与故障恢复运行手册 (Runbook)

> **版本**: v5.3
> **维护人**: DevOps Team
> **最后更新**: 2026-06-16

当主 LLM 供应商（如 SiliconFlow）发生故障、服务降级或 SLA 违标时，系统管理员应执行本手册进行快速切换与灾备。

## 一、支持的供应商配置

本系统支持 `siliconflow`、`deepseek` 和 `openai` (任意兼容标准 API 的提供商)。

| 供应商 (LLM_PROVIDER) | 默认 Base URL | 推荐模型 (OPENAI_MODEL) |
|---|---|---|
| `siliconflow` | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` |
| `deepseek` | `https://api.deepseek.com/v1` | `deepseek-chat` |
| `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` |

---

## 二、熔断器与降级策略

### 2.1 熔断器配置

```bash
CIRCUIT_BREAKER_FAIL_THRESHOLD=5    # 连续失败次数阈值（与 core/config.py 一致）
CIRCUIT_BREAKER_RECOVERY_TIME=60    # 恢复尝试间隔（秒）
LLM_ROUTER_TIMEOUT=4.0              # LLM 调用超时（秒）
```

### 2.2 自动降级流程

```
LLM API 挂了（连续 5+ 次失败）
    ↓
熔断器打开（circuit_breaker_state = "open"）
    ↓
RuleBasedLLM 兜底响应（规则引擎，非 AI）
    ↓
降级文案："抱歉，服务暂时繁忙，请稍后重试。"
```

### 2.3 降级期间监控

```bash
# 查看熔断器状态
curl -s http://localhost:8000/api/monitoring/metrics | jq '.circuit_breaker'

# 查看 LLM 错误率
curl -s http://localhost:8000/api/monitoring/metrics | jq '.llm_errors'
```

---

## 三、紧急切换步骤

### 1. 备份当前配置

```bash
cp .env.prod.generated .env.prod.generated.bak.$(date +%Y%m%d%H%M%S)
```

### 2. 修改配置文件

打开生产环境 `.env` 或 `.env.prod.generated`，替换以下配置项：

```bash
# 切换到灾备提供商（例如 deepseek）
LLM_PROVIDER=deepseek
OPENAI_API_KEY=sk-your-deepseek-api-key
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

### 3. 重启应用服务

如果是 Docker Compose 部署：

```bash
docker compose restart app
```

### 4. 验证切换成功

```bash
# 健康检查
curl -s http://localhost:8000/health | jq '.llm_provider'

# 测试请求
curl -s -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "你好", "session_id": "test"}' | jq '.response'
```

---

## 四、回滚流程

如切换后问题未解决或出现新问题，执行回滚：

```bash
# 1. 恢复备份
cp .env.prod.generated.bak.20260616 .env.prod.generated

# 2. 重启
docker compose restart app

# 3. 验证
curl -s http://localhost:8000/health | jq '.llm_provider'
```

---

## 五、在线故障演练（Chaos Drill）

为确保灾备预案的有效性，建议**每季度进行一次**主备切换演练：

- [ ] 1. **宣布切换**：在通知群（Slack/钉钉）发送切换公告
- [ ] 2. **执行切换**：参考"紧急切换步骤"配置灾备提供商
- [ ] 3. **健康检查**：检查 API 服务 `/api/health` 与并发请求延迟
- [ ] 4. **回滚演练**：切换回原主提供商，确保服务平稳
- [ ] 5. **更新本文档**：如有变更，记录演练结果

---

## 六、供应商 SLA 参考

| 供应商 | 正常运行时间 | 典型延迟 | 熔断建议 |
|--------|------------|---------|---------|
| SiliconFlow | 99.5% | 2-5s | 连续 5 次超时 |
| DeepSeek | 99.0% | 3-8s | 连续 5 次超时 |
| OpenAI | 99.9% | 1-3s | 连续 10 次超时 |

---

## 七、相关文档

- [密钥轮换脚本](../../scripts/rotate_secrets.py)
- [监控配置](../../monitoring/)
- [LLM 客户端实现](../../llm/client.py)
- [熔断器实现](../../core/monitoring.py)（`class CircuitBreaker`）