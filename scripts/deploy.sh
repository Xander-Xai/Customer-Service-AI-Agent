#!/usr/bin/env bash
# ============================================================
# 客服 AI Agent — 生产部署脚本（v3.9 生产就绪版）
# 用法: ./scripts/deploy.sh [prod|dev]
# ============================================================
set -euo pipefail

ENV="${1:-dev}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

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

# ── 停止旧容器 ──
echo "⏹️  停止旧容器..."
docker compose down --remove-orphans 2>/dev/null || true

# ── 构建并启动 ──
if [ "${ENV}" = "prod" ]; then
    echo "🚀 启动生产环境（Nginx + App + Redis + Prometheus + Grafana）..."
    docker compose up -d --build nginx app redis prometheus grafana
    echo ""
    echo "✅ 部署完成！"
    echo ""
    echo "📌 服务地址:"
    echo "   应用（通过 Nginx）: https://localhost"
    echo "   应用直连:           http://localhost:8000"
    echo "   Grafana:            http://localhost:3000 (admin / ${GRAFANA_PASSWORD:-admin})"
    echo "   Prometheus:         http://localhost:9090"
    echo ""
    echo "📌 健康检查:"
    echo "   curl -k https://localhost/api/health"
    echo "   curl http://localhost:8000/api/health"
    echo ""
    echo "📌 查看日志:"
    echo "   docker compose logs -f app"
    echo "   docker compose logs -f nginx"
else
    echo "🔧 启动开发环境（App + Redis）..."
    docker compose up -d --build app redis
    echo ""
    echo "✅ 开发环境已启动！"
    echo ""
    echo "📌 服务地址:"
    echo "   应用: http://localhost:8000"
    echo ""
    echo "📌 健康检查:"
    echo "   curl http://localhost:8000/api/health"
fi
