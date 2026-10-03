.PHONY: help dev dev-docker test test-mcp test-cov lint format prod prod-down prod-build clean env-check db-migrate db-upgrade backup canary scale scale-down monitoring-up eval-rag rag-eval-649 rag-eval-649-preflight rag-eval-649-smoke rag-eval-import audit-docs openapi-check facts runtime-e2e runtime-chaos runtime-verify mcp-verify

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
test: env-test ## 运行测试
	@echo "🧪 运行测试..."
	python3 -m pytest tests/ -x -v --tb=short

test-cov: env-test ## 运行测试（带覆盖率）
	@echo "🧪 运行测试（覆盖率）..."
	python3 -m pytest tests/ -x -v --tb=short --cov=. --cov-report=term-missing --cov-report=html:htmlcov

test-fast: env-test ## 快速测试（跳过慢测试）
	@echo "⚡ 快速测试..."
	python3 -m pytest tests/ -x -v --tb=short -m "not slow"

test-mcp: env-test ## MCP 工具适配验证（本地 fake MCP server 真实子进程端到端契约）
	@echo "🔌 MCP 工具适配测试..."
	# `-rs` 会把 skip 原因打出来：官方 mcp SDK 未装时这里会 SKIPPED 而不是静默
	# 通过 —— 「没跑」不能被读成「跑过了」。fake MCP server 是本地子进程，不依赖
	# 任何外部公开 MCP 服务。
	python3 -m pytest tests/integration/test_mcp_contract_e2e.py -v --tb=short -rs -p no:cacheprovider

# ===== 代码质量 =====
lint: ## 代码检查（ruff）
	ruff check .
	ruff format --check .

format: ## 代码格式化
	ruff format .
	ruff check --fix .

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
.PHONY: audit-docs openapi-check facts

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
prod: env-prod ## 生产部署
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
monitoring-up: ## 启动 Loki 日志聚合
	@echo "📊 启动 Loki + Promtail..."
	docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.monitoring.yml up -d loki promtail

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
