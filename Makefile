.PHONY: help dev dev-docker test test-mcp test-cov lint format prod prod-down prod-build clean env-check db-migrate db-upgrade backup canary scale scale-down monitoring-up monitoring-token metrics-exposure-check metrics-exposure-verify alert-rules-test agent-eval agent-eval-contract agent-eval-annotation-status agent-eval-cases agent-eval-real rag-gold-validate rag-gold-review rag-gold-known-item rag-gold-reviewed-eval rag-gold-reviewed-status rag-ablation runtime-report perf-evidence eval-rag rag-eval-649 rag-eval-649-preflight rag-eval-649-smoke rag-eval-import audit-docs openapi-check facts runtime-e2e runtime-chaos runtime-verify mcp-verify demo-offline

# reviewed-gold 人工标注数据集（人工生产，默认路径；可用 GOLD_LABELS=... 覆盖）
GOLD_LABELS ?= tests/eval/gold_labels/reviewed_gold.jsonl

# ===== 默认目标 =====
help: ## 显示帮助
	@echo ""
	@echo "  多智能体客服系统 — 环境管理命令"
	@echo "  ═══════════════════════════════════════"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ===== 开发环境 =====
dev: env-dev ## 本地开发启动（uvicorn 热重载）
	@echo "🚀 启动开发服务器..."
	uvicorn api.app_factory:app --host 0.0.0.0 --port 8000 --reload --reload-dir . --log-level debug

dev-https: env-dev ## 本地 HTTPS 开发启动（支持麦克风等需要安全上下文的功能）
	@echo "🔒 启动 HTTPS 开发服务器..."
	uvicorn api.app_factory:app --host 0.0.0.0 --port 8444 --reload --reload-dir . --log-level info --ssl-keyfile .certs/key.pem --ssl-certfile .certs/cert.pem

dev-docker: env-dev ## Docker 开发环境（自动加载 override）
	@echo "🐳 启动 Docker 开发环境..."
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.override.yml up app --build

# ===== 测试 =====
test: env-test ## 运行测试（默认离线：不出网、不需要真实 API Key）
	@echo "🧪 运行测试（默认离线 lane：EMBEDDING/RERANKER/STT/TTS provider=local）..."
	python3 -m pytest tests/ -x -v --tb=short

test-cov: env-test ## 运行测试（带覆盖率，默认离线）
	@echo "🧪 运行测试（覆盖率，离线 lane）..."
	python3 -m pytest tests/ -x -v --tb=short --cov=. --cov-report=term-missing --cov-report=html:htmlcov

test-fast: env-test ## 快速测试（跳过慢测试，默认离线）
	@echo "⚡ 快速测试..."
	python3 -m pytest tests/ -x -v --tb=short -m "not slow"

test-real-providers: ## 真实 provider 验证（issue #52：默认不执行，需要真实凭据）
	@echo "⚠️  真实 provider lane —— 需要真实 API Key，且会发起外网请求。"
	@echo "    凭据从环境变量读取：OPENAI_API_KEY / EMBEDDING_API_KEY / RERANKER_API_KEY"
	@echo "    默认测试（make test）不依赖这些凭据，也不发起外网请求。"
	@test -n "$$EMBEDDING_API_KEY" || { \
		echo "❌ EMBEDDING_API_KEY 未设置。默认离线 lane 请用 'make test'。"; exit 1; }
	@test -n "$$RERANKER_API_KEY" || { \
		echo "❌ RERANKER_API_KEY 未设置。默认离线 lane 请用 'make test'。"; exit 1; }
	EMBEDDING_PROVIDER=remote RERANKER_PROVIDER=remote STT_PROVIDER=remote TTS_PROVIDER=remote \
		python3 -m pytest tests/ -v --tb=short -m "real_provider or real_llm"

test-mcp: env-test ## MCP 工具适配验证（本地 fake MCP server 真实子进程端到端契约）
	@echo "🔌 MCP 工具适配测试..."
	# `-rs` 会把 skip 原因打出来：官方 mcp SDK 未装时这里会 SKIPPED 而不是静默
	# 通过 —— 「没跑」不能被读成「跑过了」。fake MCP server 是本地子进程，不依赖
	# 任何外部公开 MCP 服务。
	python3 -m pytest tests/integration/test_mcp_contract_e2e.py -v --tb=short -rs -p no:cacheprovider

demo-offline: env-test ## 一键离线演示（Mock LLM / 无 API Key / 输出可审计证据卡）
	@echo "🎬 一键离线演示（离线上网守卫；无需 API Key）..."
	@python3 scripts/demo_offline.py

# ===== 代码质量 =====
# canonical lint 工具链是 requirements-dev.txt / .pre-commit-config.yaml 里
# pin 的 ruff（当前 0.4.0）。若 PATH 上是一个更新的 ruff，同一份代码会因新
# 规则产生假失败。优先用项目 venv 里 pin 的 ruff，保持本地与 CI 一致。
RUFF ?= $(shell if [ -x .venv/bin/ruff ]; then echo .venv/bin/ruff; else echo ruff; fi)

lint: ## 代码检查（ruff）
	$(RUFF) check .
	$(RUFF) format --check .

format: ## 代码格式化
	$(RUFF) format .
	$(RUFF) check --fix .

# ===== RAG 评估 =====
eval-rag: ## RAG 检索质量评估（rag-eval-649 的兼容 alias；此为唯一正式评测入口）
	@$(MAKE) --no-print-directory rag-eval-649

rag-eval-649: ## RAG 649 正式评测（canonical formal command: preflight → 4 实验 ablation → evidence artifact）
	@echo "📊 RAG 649 evidence 评测（先确认语料已导入: make rag-eval-import）..."
	python3 scripts/evaluate_rag.py

rag-eval-649-preflight: ## RAG 649 评测 preflight gate（Qdrant/embedding/reranker/BM25）
	python3 scripts/evaluate_rag.py --preflight-only

rag-eval-649-smoke: ## RAG 649 评测冒烟（前 16 条，subset_run=true）
	python3 scripts/evaluate_rag.py --limit 16

rag-eval-import: ## 导入评测语料（幂等）并重建 BM25（写入 import manifest）
	python3 scripts/import_eval_corpus.py

# ===== 文档/运行时事实工具 =====
.PHONY: audit-docs openapi-check facts tls-check tls-local-cert

# ===== TLS 物料（issue #57）=====
# 证书是**文件**不是环境变量，无法用 compose 的 `${VAR:?}` 在 config 阶段拦下，
# 所以这一层由宿主机脚本承担：存在 / 非空 / 可解析 / 证书私钥配对 / 有效期 /
# 自签名策略。缺失时在 `docker compose up` **之前**失败并说明需要提供什么。
tls-check: ## 校验生产 TLS 物料（deploy/nginx/ssl），缺失/不匹配/过期即 fail fast
	@python3 scripts/check_tls_material.py

tls-local-cert: ## 生成本地自签证书（LOCAL DEVELOPMENT ONLY，禁止用于生产）
	@python3 scripts/generate_local_selfsigned_cert.py --target nginx --force

audit-docs: ## 文档一致性审计（链接/引用/配置/OpenAPI/基准）
	@echo "🔍 文档一致性审计..."
	python3 scripts/audit_doc_consistency.py

openapi-check: ## 校验 docs/openapi.json 与 app.openapi() 一致
	@echo "🔍 OpenAPI 快照一致性..."
	python3 scripts/generate_openapi.py --check

facts: ## 输出当前 runtime 事实 JSON（版本/模型/路径数/基准查询数）
	@echo "🧾 当前 runtime 事实..."
	python3 scripts/project_facts.py

# ===== 分布式 Agent Runtime（真实 PG + Redis 验收）=====
#
# 这些目标需要**真实** PostgreSQL 与 Redis。默认连接本机；用
# TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 覆盖（CI 里指向 service container）。
# 环境缺失时目标会 FAIL 而不是静默 skip —— 免得"没跑"被当成"通过"。

.PHONY: runtime-e2e runtime-chaos runtime-verify runtime-replay-help mcp-verify

RUNTIME_DB_URL ?= postgresql://postgres:postgres@localhost:5432/csai_runtime_test
RUNTIME_REDIS_URL ?= redis://localhost:6379

runtime-e2e: ## Runtime 端到端验收（checkpoint/thread 隔离/queue/worker/retry/DLQ/幂等）
	@echo "🚦 分布式 Runtime E2E（真实 PostgreSQL + Redis）..."
	@TEST_DISTRIBUTED_DB_URL="$(RUNTIME_DB_URL)" TEST_REDIS_URL="$(RUNTIME_REDIS_URL)" \
		python3 -m pytest tests/integration/runtime -q -p no:cacheprovider
	@echo "✅ runtime-e2e PASS"

runtime-chaos: ## 混沌验收：worker kill -9 -> lease 过期 -> checkpoint 恢复 -> 副作用仅一次
	@echo "💥 分布式 Runtime 混沌验收..."
	@mkdir -p artifacts/runtime
	@TEST_DISTRIBUTED_DB_URL="$(RUNTIME_DB_URL)" TEST_REDIS_URL="$(RUNTIME_REDIS_URL)" \
		python3 scripts/test_worker_crash_recovery.py \
		--output artifacts/runtime/chaos-$$(date -u +%Y%m%dT%H%M%SZ).json
	@echo "✅ runtime-chaos PASS"

runtime-verify: ## 生成 runtime 能力证据报告（结构化 JSON）
	@echo "🧾 分布式 Runtime 能力验证..."
	@TEST_DISTRIBUTED_DB_URL="$(RUNTIME_DB_URL)" TEST_REDIS_URL="$(RUNTIME_REDIS_URL)" \
		python3 scripts/verify_distributed_runtime.py

runtime-replay-help: ## 查看 DLQ 重放用法
	@python3 scripts/replay_dead_run.py --help

mcp-verify: ## 生成 MCP 端到端契约证据报告（结构化 JSON，本地 fake server）
	@echo "🧾 MCP 端到端契约验证..."
	@python3 scripts/verify_mcp_contract.py

# ===== 知识库 & 基准测试 =====
.PHONY: benchmark generate-knowledge-base component-count

## 执行所有基准测试（缓存/延迟/A-B/成本/预取/分层命中率）
benchmark:
	@echo "📊 执行全量基准测试..."
	@mkdir -p reports
	@for script in benchmark_cache benchmark_latency benchmark_ab_test benchmark_cost benchmark_prefetch benchmark_cache_hierarchy; do \
		echo "  [$$script]"; \
		python3 scripts/$$script.py 2>&1 || echo "  ⚠️ $$script 运行失败（可能缺少依赖）"; \
		echo ""; \
	done
	@echo "=== 所有基准测试完成 ==="
	@ls -la reports/

## 生成 5000+ 条知识库文档
generate-knowledge-base:
	@echo "📦 生成知识库数据..."
	python3 scripts/generate_knowledge_base.py --validate

## DI 容器组件计数
component-count:
	@echo "📊 DI 容器活跃组件数:"
	@python3 scripts/_component_count.py 2>&1 | grep -v "WARNING\|INFO\|DEBUG\|^$$" || echo "  ⚠️ 容器不可用"

# ===== 生产环境 =====
prod: env-prod tls-check ## 生产部署（先校验 TLS 物料，缺失即 fail fast，不拉起容器）
	@echo "🚀 启动生产环境..."
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml up -d --build

prod-down: ## 停止生产环境
	@echo "🛑 停止生产环境..."
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml down

prod-build: env-prod ## 仅构建生产镜像
	@echo "🔨 构建生产镜像..."
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml build

prod-logs: ## 查看生产日志
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml logs -f app

prod-ps: ## 查看生产服务状态
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml ps

# ===== 环境切换 =====
env-dev: ## 切换到开发环境配置
	@if [ ! -f .env.dev ]; then echo "❌ .env.dev 不存在"; exit 1; fi
	@cp .env.dev .env
	@echo "✅ 已切换到开发环境 (.env.dev)"

env-prod: ## 切换到生产环境配置
	@if [ ! -f .env.prod ]; then echo "❌ .env.prod 不存在"; exit 1; fi
	@cp .env.prod .env
	@echo "✅ 已切换到生产环境 (.env.prod)"

env-test: ## 切换到测试环境配置
	@if [ ! -f .env.test ]; then echo "❌ .env.test 不存在"; exit 1; fi
	@cp .env.test .env
	@echo "✅ 已切换到测试环境 (.env.test)"

env-check: ## 显示当前环境配置摘要
	@if [ -f .env ]; then \
		echo ""; \
		echo "📋 当前 .env 环境摘要:"; \
		echo "  ─────────────────────────────"; \
		echo "  DEV_MODE:          $$(grep -E '^DEV_MODE=' .env | cut -d= -f2 || echo '未设置')"; \
		echo "  API_KEY_ENABLED:   $$(grep -E '^API_KEY_ENABLED=' .env | cut -d= -f2 || echo '未设置')"; \
		echo "  LOG_LEVEL:         $$(grep -E '^LOG_LEVEL=' .env | cut -d= -f2 || echo '未设置')"; \
		echo "  LOG_FORMAT:        $$(grep -E '^LOG_FORMAT=' .env | cut -d= -f2 || echo '未设置')"; \
		echo "  ERP_MODE:          $$(grep -E '^ERP_MODE=' .env | cut -d= -f2 || echo '未设置')"; \
		echo "  SESSION_BACKEND:   $$(grep -E '^SESSION_STORAGE_BACKEND=' .env | cut -d= -f2 || echo '未设置')"; \
		echo "  REDIS_URL:         $$(grep -E '^REDIS_URL=' .env | cut -d= -f2 || echo '未设置')"; \
		echo ""; \
	else \
		echo "⚠️  .env 文件不存在，请先运行: make env-dev 或 make env-prod"; \
	fi

# ===== 数据库迁移 =====
db-migrate: ## 创建新的 Alembic 迁移（用法: make db-migrate MSG="描述"）
	@echo "📝 创建数据库迁移..."
	alembic revision --autogenerate -m "$(MSG)"

db-upgrade: ## 执行数据库迁移（升级到最新）
	@echo "⬆️  执行数据库迁移..."
	alembic upgrade head

db-downgrade: ## 回退一次数据库迁移
	@echo "⬇️  回退数据库迁移..."
	alembic downgrade -1

db-history: ## 查看迁移历史
	alembic history

# ===== 备份 =====
backup: ## 执行数据备份
	@echo "💾 执行数据备份..."
	@source .env 2>/dev/null || true; bash scripts/backup.sh ./backups

# ===== 金丝雀部署 =====
canary: env-prod ## 金丝雀部署（90/10 流量分割）
	@echo "🐤 启动金丝雀部署..."
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml -f deploy/compose/docker-compose.canary.yml up -d --build

canary-down: ## 停止金丝雀部署
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml -f deploy/compose/docker-compose.canary.yml down

# ===== 水平扩展 =====
scale: env-prod ## 水平扩展（用法: make scale N=3）
	@echo "📈 扩展到 $(N) 个实例..."
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml -f deploy/compose/docker-compose.scale.yml up -d --build --scale app=$(N)

scale-down: ## 恢复单实例
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml up -d --scale app=1

# ===== 监控增强 =====
monitoring-token: ## 生成 Prometheus 抓取凭据（fail-closed：缺失/占位符直接失败）
	@python3 scripts/generate_monitoring_token.py

monitoring-up: monitoring-token ## 启动完整监控栈（Prometheus + Grafana + Alertmanager + Loki + Promtail）
	@echo "📊 启动 Prometheus + Grafana + Alertmanager + Loki + Promtail..."
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.monitoring.yml \
		up -d prometheus alertmanager grafana loki promtail
	@echo "✅ 监控栈已拉起。验证指标是否真的被抓到：make metrics-exposure-check"

metrics-exposure-check: ## 指标暴露链路契约（告警/Grafana 引用的每个指标名都可达）
	@python3 -m pytest tests/unit/test_metrics_exposure_contract.py tests/unit/test_metrics_multiprocess.py tests/unit/test_compose_metrics_topology.py -v --tb=short -p no:cacheprovider

alert-rules-test: monitoring-token ## 官方 promtool 规则单测（DLQ 告警是否真会 firing / 是否会 latching）
	@echo "🧪 promtool check config + test rules（真实 Prometheus 镜像）..."
	@docker run --rm \
		-v "$(PWD)/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
		-v "$(PWD)/monitoring/alert_rules.yml:/etc/prometheus/rules/alert_rules.yml:ro" \
		-v "$(PWD)/deploy/monitoring/secrets:/etc/prometheus/secrets:ro" \
		--entrypoint promtool prom/prometheus:v2.51.0 check config /etc/prometheus/prometheus.yml
	@docker run --rm -v "$(PWD)/monitoring:/etc/prometheus:ro" \
		--entrypoint promtool prom/prometheus:v2.51.0 test rules /etc/prometheus/alert_rules_test.yml

metrics-exposure-verify: ## 真实 Prometheus 端到端：DLQ 告警是否真的会触发（不依赖本地 token）
	@bash scripts/verify_metrics_exposure.sh

# ===== Agent 行为评测（Agent Eval V1）=====
# 真实编译图 + 脚本化 LLM，零出网、可重复。它**不是**单元测试通过率：
# 分母是 case 数，失败 case 照常计入，NOT_MEASURED 不会被渲染成 0%。
# 见 docs/reference/agent-evaluation.md
agent-eval: ## Agent 行为评测（真实图回放 + evidence artifact）
	@python3 scripts/evaluate_agent.py

agent-eval-contract: ## agent-eval 契约守卫（指标名 / 证据边界 / 降级标记与生产代码同步）
	@python3 -m pytest tests/unit/test_agent_eval_contract.py tests/unit/test_hitl_real_graph_gate.py -v --tb=short -p no:cacheprovider

agent-eval-annotation-status: ## 标注状态报告（human_confirmed 占比 + 各指标分母）
	@python3 scripts/evaluate_agent.py --print-annotation-status

# 真实模型 lane：默认**不**调用外部 provider。凭据只能从环境变量注入
# （AGENT_EVAL_REAL_PROVIDER_API_KEY / OPENAI_API_KEY），且必须同时满足
# AGENT_EVAL_REAL_PROVIDER_AUTHORIZED=1 与 --i-authorize-external-calls。
# 缺任一条件 -> NOT_MEASURED，零外网请求。真实结果单独落盘，不与 scripted 混合。
agent-eval-real: ## 真实模型评测 lane（默认 NOT_MEASURED；需凭据 + 显式授权）
	@python3 scripts/evaluate_agent_real.py $(ARGS)

agent-eval-cases: ## 重新生成候选数据集（全部标为 llm_candidate，需人工确认）
	@python3 scripts/generate_agent_eval_cases.py

# ===== RAG 标注与消融 =====
rag-gold-validate: ## RAG gold 标注契约校验（rag-gold-label/v1，含语料存在性检查）
	@python3 scripts/validate_gold_labels.py tests/eval/gold_labels/template.jsonl --no-corpus
	@python3 scripts/validate_gold_labels.py tests/eval/gold_labels/known_item_gold.jsonl \
		--corpus data/knowledge_base/knowledge_base_5000.jsonl

rag-gold-review: ## 生成待人工标注工作清单（含语料存在性与覆盖率统计）
	@python3 scripts/build_rag_gold_review_worklist.py

rag-gold-known-item: ## 生成 known-item CONSTRUCTED gold（构造保证相关，非人工判定）
	@python3 scripts/build_rag_known_item_gold.py

rag-gold-reviewed-eval: ## RAG reviewed-gold 正式评测（只消费人工 JUDGED 标签；无 JUDGED 即 NOT_MEASURABLE，fail closed）
	@python3 scripts/evaluate_rag_reviewed_gold.py --gold-labels $(GOLD_LABELS)

rag-gold-reviewed-status: ## reviewed-gold 标注状态/质检（离线，不触 Qdrant；可核对 JUDGED / 完整判断 / 排除分母）
	@python3 scripts/evaluate_rag_reviewed_gold.py \
		--gold-labels $(GOLD_LABELS) --no-corpus --summary-only

rag-ablation: ## 4 组检索消融（可测的给数字，不可用的输出 BLOCKED，绝不估算）
	@python3 scripts/run_rag_ablation.py --negative-control

# ===== 运行时与性能证据 =====
runtime-report: ## 汇总分布式运行时验收报告（副作用按真实执行次数计）
	@TEST_DISTRIBUTED_DB_URL="$(RUNTIME_DB_URL)" TEST_REDIS_URL="$(RUNTIME_REDIS_URL)" \
		python3 scripts/report_distributed_runtime.py

perf-evidence: ## 性能/成本门禁与证据（无真实 provider 时结构化 BLOCKED）
	@python3 scripts/measure_performance.py --offline-checks

# ===== 部署（脚本方式）=====
deploy-prod: ## 使用 deploy.sh 部署生产环境
	@echo "🚀 通过脚本部署..."
	bash scripts/deploy.sh prod

# ===== 清理 =====
clean: ## 清理构建产物
	@echo "🧹 清理..."
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .coverage
	@echo "✅ 清理完成"

# ===== 依赖快照（非权威）=====
# 依赖契约分层（每层语义不同，不要把它们混为一谈）：
#   Deployment（生产）  : requirements.txt + Dockerfile 是 canonical production contract。
#   CI Lane A（coverage reproducibility）: requirements.txt + pinned pytest /
#                         pytest-asyncio / pytest-cov / coverage。
#   Dev compatibility Lane B: requirements.txt + requirements-dev.txt。
#   requirements-lock.txt       : HISTORICAL / NON-AUTHORITATIVE 快照（保留其 header，
#                                 工具不会重写它）。
#   requirements-lock.local.txt : untracked 本地快照（gitignored）。
# `make lock` 只把本地环境快照写到未跟踪的 requirements-lock.local.txt。
lock: ## 生成本地依赖快照（非权威，不用于部署）
	@echo "🔒 生成 requirements-lock.local.txt 本地快照（非权威）..."
	pip install -r requirements.txt 2>/dev/null
	pip freeze --exclude-editable > requirements-lock.local.txt
	@echo "⚠️  这是本地快照，不是可复现 lock；部署请使用 requirements.txt"
	@echo "ℹ️  requirements-lock.txt 为冻结的历史快照，本命令不会重写它"

# ===== v6.0: Qdrant 运维 =====

# 启动 Qdrant
.PHONY: qdrant-start
qdrant-start:
	docker compose -f deploy/compose/docker-compose.yml up -d qdrant

# 停 Qdrant
.PHONY: qdrant-stop
qdrant-stop:
	docker compose -f deploy/compose/docker-compose.yml stop qdrant

# 数据迁移：ChromaDB → Qdrant
.PHONY: migrate-qdrant
migrate-qdrant:
	python3 scripts/migrate_chroma_to_qdrant.py

# 切换为 Qdrant Only 模式（输出提示）
.PHONY: use-qdrant
use-qdrant:
	@echo "在 .env 中设置:"
	@echo "  VECTOR_DB_MODE=qdrant_only"
	@echo "  QDRANT_HOST=localhost"
	@echo "  QDRANT_PORT=6333"
	@echo "然后执行: make dev"

# Qdrant 健康检查
.PHONY: qdrant-health
qdrant-health:
	curl -s http://localhost:6333/healthz | python3 -m json.tool

# ===== Observability: live OTLP Collector =====
# 只证明「已有 semantic span → SDK → OTLPSpanExporter → 网络 → 真实 Collector」。
# 不证明生产 trace 传播，也不证明存在持久化/可查询 trace 后端
# （Collector 只有 debug exporter，不存储）。
otel-collector-smoke: ## 真跑一次 OTLP Collector：启动→健康就绪→发 span→flush→校验→出证据→关闭
	@echo "🔭 OTLP Collector smoke（真实 otel/opentelemetry-collector，traces only）..."
	@python3 scripts/otel_collector_smoke.py
	@echo "✅ otel-collector-smoke PASS（evidence: artifacts/observability/otel-collector-*/report.json）"

.PHONY: otel-collector-up
otel-collector-up: ## 仅启动 traces-only Collector（后台常驻）
	docker compose -f deploy/compose/docker-compose.otel.yml up -d otel-collector

.PHONY: otel-collector-down
otel-collector-down: ## 关闭本项目的 Collector（不影响其它容器）
	docker rm -f customer-service-otel-collector 2>/dev/null || true
