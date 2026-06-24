.PHONY: help dev dev-docker test test-cov lint format prod prod-down prod-build clean env-check db-migrate db-upgrade backup canary scale scale-down monitoring-up eval-rag

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
	python -m pytest tests/ -x -v --tb=short

test-cov: env-test ## 运行测试（带覆盖率）
	@echo "🧪 运行测试（覆盖率）..."
	python -m pytest tests/ -x -v --tb=short --cov=. --cov-report=term-missing --cov-report=html:htmlcov

test-fast: env-test ## 快速测试（跳过慢测试）
	@echo "⚡ 快速测试..."
	python -m pytest tests/ -x -v --tb=short -m "not slow"

# ===== 代码质量 =====
lint: ## 代码检查（ruff）
	ruff check .
	ruff format --check .

format: ## 代码格式化
	ruff format .
	ruff check --fix .

# ===== RAG 评估 =====
eval-rag: ## RAG 检索质量评估（基于 500+ 评测集）
	@echo "📊 RAG 检索质量评估..."
	python3 scripts/evaluate_rag.py

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
	@python3 -c "\
	import os, sys; \
	sys.path.insert(0, '.'); \
	os.environ['DEV_MODE'] = 'true'; \
	os.environ['QDRANT_HOST'] = 'localhost'; \
	os.environ['REDIS_URL'] = 'redis://localhost:6379/0'; \
	from core.container import ServiceContainer; \
	c = ServiceContainer(); \
	import asyncio; \
	asyncio.run(c.initialize()); \
	svc_count = sum(1 for _ in filter(lambda x: not x.startswith('_') and not callable(getattr(c, x, lambda: None)), dir(c))); \
	print(f'  服务组件: {svc_count}'); \
	" 2>/dev/null || echo "  ⚠️ 容器不可用（依赖服务未运行）"

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

# ===== 依赖锁定 =====
lock: ## 生成依赖锁定文件
	@echo "🔒 生成 requirements-lock.txt..."
	pip install -r requirements.txt 2>/dev/null
	pip freeze --exclude-editable > requirements-lock.txt
	@echo "✅ requirements-lock.txt 已生成（请检查并提交）"

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
