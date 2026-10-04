# LLM 供应商快速切换与故障恢复运行手册 (Runbook)

> **版本**: v6.3（2026-09-30 与 runtime 事实重新收敛）
> **维护人**: DevOps Team
> **事实来源**: `core/config.py` / `llm/client.py` / `core/container.py` / `deploy/compose/docker-compose.yml`
> **证据边界**: 本手册只描述当前代码行为。供应商 SLA、延迟与价格在当前 checkout 中属于
> `NOT_VERIFIED` / `NOT_MEASURED`，不得当作当前事实引用（历史估算见
> [docs/reference/model-comparison.md](../reference/model-comparison.md) 历史章节）。

当主 LLM 供应商（如 SiliconFlow）发生故障、服务降级或响应异常时，系统管理员应执行本手册进行快速切换与灾备。

## 一、当前默认与支持的供应商

默认模型由 `core/config.py` 决定：`OPENAI_MODEL` 默认 **`Qwen/Qwen3-8B`**，
provider interface 为 OpenAI-compatible HTTP API。`LLM_PROVIDER` 仅决定语义标签，
真正生效的是 `OPENAI_BASE_URL` + `OPENAI_API_KEY` + `OPENAI_MODEL`。

| 供应商 (LLM_PROVIDER) | 默认 Base URL | 当前默认模型 | 可选模型示例 |
|---|---|---|---|
| `siliconflow`（默认） | `https://api.siliconflow.cn/v1` | `Qwen/Qwen3-8B` | 其他 SiliconFlow 托管模型 |
| `deepseek` | `https://api.deepseek.com/v1` | `deepseek-chat` | — |
| `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` | — |
| `custom` | 自定义 | 自定义 | 任意 OpenAI-compatible 端点 |

关键环境变量（runtime fallback 见 `core/config.py`）：

```text
LLM_PROVIDER        默认 siliconflow
OPENAI_BASE_URL     默认 https://api.siliconflow.cn/v1
OPENAI_MODEL        默认 Qwen/Qwen3-8B
OPENAI_API_KEY      必填
LLM_MAX_TOKENS      runtime fallback 4096（.env.example 模板推荐 8192）
HTTP_TIMEOUT        runtime fallback 15s（.env.example 模板推荐 30s）
LLM_ROUTER_TIMEOUT  runtime fallback 4.0s（.env.example / Compose 模板推荐 8.0s）
```

> Compose (`deploy/compose/docker-compose.yml`) 的 `OPENAI_MODEL` 默认值与
> `core/config.py` 一致；`${OPENAI_MODEL:-Qwen/Qwen3-8B}`。

---

## 二、熔断器与降级策略

### 2.1 熔断器配置

```bash
CIRCUIT_BREAKER_FAIL_THRESHOLD=5    # 连续失败次数阈值（与 core/config.py 默认一致）
CIRCUIT_BREAKER_RECOVERY_TIME=60    # 恢复尝试间隔（秒）
LLM_ROUTER_TIMEOUT=8.0              # 模板推荐值；runtime fallback 为 4.0s
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
# 查看熔断器状态（需要 Admin Token / supervisor+ 权限时按部署环境提供认证）
curl -s http://localhost:8000/api/circuit-breaker
curl -s http://localhost:8000/api/health
```

健康检查端点是 `/api/health`（无根级 `/health` 端点）。

---

## 三、紧急切换步骤

### 1. 备份当前配置

```bash
cp .env .env.bak.$(date +%Y%m%d%H%M%S)
```

### 2. 修改配置文件

打开生产环境 `.env`，替换以下配置项：

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
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml restart app
```

### 4. 验证切换成功

```bash
# 健康检查（/api/health，不是 /health）
curl -s http://localhost:8000/api/health

# 只看 LLM 段：确认 provider 已切换，且 key 可用、没有被降级到规则引擎
curl -s http://localhost:8000/api/health | jq '.components.llm'
# 期望（切换成功且凭据有效）：
# {
#   "configured": true,
#   "key_usable": true,          # 与运行时同一个判定的结论
#   "key_valid": true,           # 既有字段，与 key_usable 同源同值
#   "key_reason": "ok",
#   "key_length": 53,
#   "provider": "deepseek",      # 应为刚切换的目标 provider
#   "implementation": "OpenAICompatibleClient",
#   "degraded": false            # true = 正在用 RuleBasedLLM 模板兜底
# }
#
# ⚠️ `degraded: true` 或 `key_usable: false` 表示 key 未被接受（占位前缀或
#    长度 < 40），进程已回落到 RuleBasedLLM —— 此时接口仍能返回内容，但那是模板，
#    不是模型。切换后请以此字段为准，不要只看 HTTP 200。
# 该判定由 core/config.py::evaluate_llm_api_key 唯一实现，健康面与运行时不会不一致；
# /api/health 不会为了确认 key 去调用 provider（无鉴权端点，不打计费接口）。

# 测试请求
curl -s -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "你好", "session_id": "test"}'
```

---

## 四、回滚流程

如切换后问题未解决或出现新问题，执行回滚：

```bash
# 1. 恢复备份
cp .env.bak.<timestamp> .env

# 2. 重启
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml restart app

# 3. 验证
curl -s http://localhost:8000/api/health
```

---

## 五、Provider 认证与证据预检

在正式切换前，使用仓库自带的两个脚本校验凭据与可测性（只输出元数据，不写密钥或响应体）：

```bash
# 1) 认证预检：最多一次 GET /v1/models?sub_type=chat 请求
#    401 = AUTH_FAILED（不可重试）；403 = FORBIDDEN（不可重试）
python3 scripts/probe_provider_auth.py

# 2) 受控证据采集（本地 fixture suite，不进行真实 provider 调用）
python3 scripts/run_production_evidence.py \
  --suite local \
  --output artifacts/evidence/local.json \
  --markdown-output artifacts/evidence/local.md

# 3) 受控 provider staging（默认 dry-run，EVAL_REAL_PROVIDER=1 才真实调用）
EVAL_REAL_PROVIDER=0 python3 scripts/run_production_evidence.py \
  --suite provider-staging --repeat 2 --warmup 1 \
  --max-requests 20 --max-input-tokens 2000 --max-output-tokens 128 \
  --estimated-cost-cap 1 \
  --output artifacts/evidence/provider-staging.json \
  --markdown-output artifacts/evidence/provider-staging.md
```

详细语义见 [docs/evaluation/production-evidence.md](../evaluation/production-evidence.md)。

---

## 六、在线故障演练（Chaos Drill）

为确保灾备预案的有效性，建议**每季度进行一次**主备切换演练：

- [ ] 1. **宣布切换**：在通知群（Slack/钉钉）发送切换公告
- [ ] 2. **执行切换**：参考"紧急切换步骤"配置灾备提供商
- [ ] 3. **健康检查**：检查 `/api/health` 与并发请求延迟
- [ ] 4. **回滚演练**：切换回原主提供商，确保服务平稳
- [ ] 5. **更新本文档**：如有变更，记录演练结果

---

## 七、供应商 SLA / 延迟 / 价格声明

本仓库当前 checkout **没有**可复现的供应商 SLA、延迟或价格测量证据。
以下字段一律标为 `NOT_VERIFIED` / `NOT_MEASURED`，禁止在运维口径中引用具体数字：

- 正常运行时间（SLA uptime）: NOT_VERIFIED
- 典型延迟 / 首 token 延迟: NOT_MEASURED
- 当前计价: NOT_VERIFIED（历史估算值仅存在于
  [model-comparison.md](../reference/model-comparison.md) 的历史章节，且仅作示例）

熔断建议保持代码默认：连续 `CIRCUIT_BREAKER_FAIL_THRESHOLD`（默认 5）次失败即降级。

---

## 八、相关文档

- [密钥轮换脚本](../../scripts/rotate_secrets.py)
- [监控配置](../../monitoring/)
- [LLM 客户端实现](../../llm/client.py)
- [熔断器实现](../../core/monitoring.py)（`class CircuitBreaker`）
- [生产证据边界](../evaluation/production-evidence.md)
- [模型选型历史记录](../reference/model-comparison.md)
