#!/usr/bin/env bash
# ============================================================
# 客服 AI Agent — 生产部署脚本（v4.2 零停机改进版）
# 用法: ./scripts/deploy.sh [prod|dev]
# ============================================================
set -euo pipefail

ENV="${1:-dev}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HEALTH_URL="http://localhost:8000/api/health"
HEALTH_TIMEOUT=60  # 健康检查超时（秒）

echo "============================================"
echo "  客服 AI Agent — 部署脚本 (${ENV})"
echo "============================================"

cd "${PROJECT_DIR}"

# ── 检查 .env ──
if [ ! -f ".env" ]; then
    echo "❌ .env 文件不存在！"
    echo "   请先运行: cp .env.example .env"
    echo "   然后编辑 .env 填入真实配置"
    exit 1
fi

# ── 检查关键环境变量 ──
source .env
_missing=()
[ -z "${OPENAI_API_KEY:-}" ] || [ "${OPENAI_API_KEY:-}" = "your_siliconflow_api_key_here" ] && _missing+=("OPENAI_API_KEY")
[ -z "${API_KEY:-}" ] || [ "${API_KEY:-}" = "your-secure-api-key-here" ] && _missing+=("API_KEY")

if [ ${#_missing[@]} -gt 0 ]; then
    echo "❌ 以下环境变量未配置或使用了默认值:"
    for v in "${_missing[@]}"; do
        echo "   - ${v}"
    done
    exit 1
fi
echo "✅ 环境变量检查通过"

# ── 生成 TLS 证书（仅首次） ──
SSL_DIR="${PROJECT_DIR}/nginx/ssl"
if [ "${ENV}" = "prod" ] && [ ! -f "${SSL_DIR}/cert.pem" ]; then
    echo "🔐 生成自签名 TLS 证书（仅用于测试，正式环境请用真实证书）..."
    mkdir -p "${SSL_DIR}"
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout "${SSL_DIR}/key.pem" \
        -out "${SSL_DIR}/cert.pem" \
        -subj "/CN=localhost/O=CustomerService/C=CN" 2>/dev/null
    echo "✅ TLS 证书已生成 → ${SSL_DIR}/"
fi

# ── 创建数据目录 ──
mkdir -p data/redis
mkdir -p backups
mkdir -p logs
mkdir -p chat_sessions
echo "✅ 数据目录已就绪"

# ── 构建新镜像（先构建，不影响运行中的容器） ──
echo "🔨 构建新镜像..."
if [ "${ENV}" = "prod" ]; then
    docker compose -f docker-compose.yml -f docker-compose.prod.yml build --no-cache app
else
    docker compose build --no-cache app
fi
echo "✅ 镜像构建完成"

# ── 滚动重启服务 ──
echo "🔄 滚动重启服务..."
if [ "${ENV}" = "prod" ]; then
    docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build nginx app redis prometheus grafana postgres
else
    docker compose up -d --no-build app redis
fi

# ── 等待健康检查通过 ──
echo "⏳ 等待服务就绪（最多 ${HEALTH_TIMEOUT}s）..."
_start=$(date +%s)
_healthy=false
while [ $(( $(date +%s) - _start )) -lt ${HEALTH_TIMEOUT} ]; do
    if curl -sf "${HEALTH_URL}" > /dev/null 2>&1; then
        _healthy=true
        break
    fi
    sleep 3
done

if [ "${_healthy}" = true ]; then
    echo "✅ 健康检查通过"
else
    echo "⚠️  健康检查超时，请检查日志: docker compose logs app"
    exit 1
fi

# ── 部署完成 ──
if [ "${ENV}" = "prod" ]; then
    echo ""
    echo "✅ 生产环境部署完成！"
    echo ""
    echo "📌 服务地址:"
    echo "   应用（通过 Nginx）: https://localhost"
    echo "   应用直连:           http://localhost:8000"
    echo "   Grafana:            http://localhost:3000 (admin / ${GRAFANA_PASSWORD})"
    echo "   Prometheus:         http://localhost:9090"
    echo "   Alertmanager:       http://localhost:9093"
    echo "   Loki:               http://localhost:3100"
    echo ""
    echo "📌 健康检查:"
    echo "   curl -k https://localhost/api/health"
    echo "   curl http://localhost:8000/api/health"
    echo ""
    echo "📌 查看日志:"
    echo "   docker compose logs -f app"
    echo "   docker compose logs -f nginx"
else
    echo ""
    echo "✅ 开发环境已启动！"
    echo ""
    echo "📌 服务地址:"
    echo "   应用: http://localhost:8000"
    echo ""
    echo "📌 健康检查:"
    echo "   curl http://localhost:8000/api/health"
fi
