# ADR-007: 当前默认 LLM（Qwen/Qwen3-8B，OpenAI-compatible Provider 接口）

**日期**：2026-09-30
**状态**：已采纳（Supersedes [ADR-003](003-qwen-default-llm.md)）
**决策者**：项目负责人

## 背景

ADR-003（2026-06-02）将 `Qwen/Qwen2.5-7B-Instruct` 选为默认 LLM。此后运行时已多次
演进（v6.0–v6.3），`core/config.py` 的实际默认模型变更为 `Qwen/Qwen3-8B`，但
部署模板与运维文档之间出现了默认模型漂移（例如 `docker-compose.yml` 仍写
`Qwen/Qwen2.5-7B-Instruct`），导致 Docker 与本地运行可能使用不同模型。
本 ADR 将当前事实固化为正式决策，并定义防止再次漂移的守卫。

## 决策

- 当前默认 LLM：**`Qwen/Qwen3-8B`**（`core/config.py::OPENAI_MODEL`）。
- Provider interface：OpenAI-compatible HTTP API（`/chat/completions`、`/models`、
  embeddings/rerank 同理），通过 `OPENAI_BASE_URL` + `OPENAI_API_KEY` + `OPENAI_MODEL`
  生效；`LLM_PROVIDER`（siliconflow / deepseek / openai / custom）只是语义标签。
- 部署模板（`.env.example`、`deploy/compose/docker-compose.yml`）的模型默认值与
  runtime fallback 统一为 `Qwen/Qwen3-8B`，不再允许 Compose 单独覆盖默认模型。

## 关键环境变量（runtime fallback 为准）

```text
LLM_PROVIDER        siliconflow
OPENAI_BASE_URL     https://api.siliconflow.cn/v1
OPENAI_MODEL        Qwen/Qwen3-8B
LLM_MAX_TOKENS      runtime fallback 4096（模板推荐 8192，属 deployment recommended value）
HTTP_TIMEOUT        runtime fallback 15s（模板推荐 30s）
LLM_ROUTER_TIMEOUT  runtime fallback 4.0s（模板推荐 8.0s）
CIRCUIT_BREAKER_FAIL_THRESHOLD  5（连续失败后降级 llm/rule_based_llm.py）
```

## 为什么更换

- v6.0 起代码默认已经切到 Qwen3-8B（`core/config.py`），本次收敛只是把文档/部署
  默认值对齐代码，避免隐式漂移。
- Qwen3-8B 与 Qwen2.5-7B 在同一 OpenAI-compatible provider interface 上工作，
  切换不改变调用协议。

## 证据与未验证项

- **有证据**（当前 checkout 代码）：
  - 默认模型/base URL/provider 标签（`core/config.py:46-50`）。
  - 熔断器 + 规则兜底行为（`llm/client.py`、`llm/rule_based_llm.py`、`core/container.py`）。
  - `scripts/probe_provider_auth.py`（认证预检）、`scripts/run_production_evidence.py`
    （受控 staging 证据采集）存在且可执行。
- **未验证 / NOT_MEASURED（不得当作当前事实）**：
  - Qwen3-8B 相对 Qwen2.5-7B 的质量提升（没有当前可复现 A/B artifact）。
  - provider 首 token 延迟、端到端延迟（本地/生产均未测量）。
  - 当前 provider 计价与月度成本（历史估算值仅存在于
    [model-comparison.md](../reference/model-comparison.md) 历史章节）。
  - 生产 SLA / FCR / 人效。

## 回滚方法

回滚即换回旧默认模型（环境变量覆盖，无需改代码）：

```bash
LLM_PROVIDER=siliconflow
OPENAI_BASE_URL=https://api.siliconflow.cn/v1
OPENAI_MODEL=Qwen/Qwen2.5-7B-Instruct
```

重启后通过 `/api/health` 验证，并运行 `python3 scripts/probe_provider_auth.py`
做认证预检。若需要恢复该默认值，应新增 ADR 说明，而不是静默改回。

## 守卫

`scripts/audit_doc_consistency.py` 的 canonical config 检查会核对
`core/config.py` / `.env.example` / `deploy/compose/docker-compose.yml` 三处的
`OPENAI_MODEL` 默认值一致性，漂移时返回非零退出码。
当前事实查询：`python3 scripts/project_facts.py`。
