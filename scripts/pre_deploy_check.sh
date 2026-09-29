#!/usr/bin/env bash
# ============================================================
# 客服 AI Agent — 生产部署前检查脚本
# 用法: ./scripts/pre_deploy_check.sh
# ============================================================
set -euo pipefail

echo "============================================"
echo "  客服 AI Agent — 生产部署前检查"
echo "============================================"
echo ""

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

check_pass() {
    echo "✅ $1"
    ((PASS_COUNT++))
}

check_fail() {
    echo "❌ $1"
    ((FAIL_COUNT++))
}

check_warn() {
    echo "⚠️  $1"
    ((WARN_COUNT++))
}

# ── 1. 环境变量文件检查 ──
echo "📋 [1/8] 检查环境配置文件..."
if [ ! -f ".env" ]; then
    check_fail ".env 文件不存在"
else
    check_pass ".env 文件存在"
    
    # 检查关键配置项
    source .env
    
    if [ -z "${OPENAI_API_KEY:-}" ] || [[ "${OPENAI_API_KEY}" == *"CHANGE_ME"* ]] || [[ "${OPENAI_API_KEY}" == *"your-"* ]]; then
        check_fail "OPENAI_API_KEY 未配置或使用占位符"
    else
        check_pass "OPENAI_API_KEY 已配置"
    fi
    
    if [ -z "${API_KEY:-}" ] || [[ "${API_KEY}" == *"CHANGE_ME"* ]] || [[ "${API_KEY}" == *"your-"* ]]; then
        check_fail "API_KEY 未配置或使用占位符"
    else
        check_pass "API_KEY 已配置"
    fi
    
    if [ -z "${JWT_SECRET:-}" ] || [[ "${JWT_SECRET}" == *"CHANGE_ME"* ]] || [[ "${JWT_SECRET}" == *"change-me"* ]]; then
        check_fail "JWT_SECRET 未配置或使用默认值"
    elif [ ${#JWT_SECRET} -lt 32 ]; then
        check_fail "JWT_SECRET 长度不足 (当前: ${#JWT_SECRET}, 要求: ≥32)"
    else
        check_pass "JWT_SECRET 已配置且强度足够"
    fi
    
    if [ -z "${SESSION_TOKEN_SECRET:-}" ] || [[ "${SESSION_TOKEN_SECRET}" == *"CHANGE_ME"* ]]; then
        check_fail "SESSION_TOKEN_SECRET 未配置或使用默认值"
    else
        check_pass "SESSION_TOKEN_SECRET 已配置"
    fi
    
    if [ -z "${REDIS_PASSWORD:-}" ] || [[ "${REDIS_PASSWORD}" == *"CHANGE_ME"* ]]; then
        check_warn "REDIS_PASSWORD 未设置强密码"
    else
        check_pass "REDIS_PASSWORD 已配置"
    fi
    
    if [ -z "${POSTGRES_PASSWORD:-}" ] || [[ "${POSTGRES_PASSWORD}" == *"CHANGE_ME"* ]]; then
        check_warn "POSTGRES_PASSWORD 未设置强密码"
    else
        check_pass "POSTGRES_PASSWORD 已配置"
    fi
    
    if [ "${DEV_MODE:-false}" = "true" ]; then
        check_fail "DEV_MODE=true，生产环境必须设置为 false"
    else
        check_pass "DEV_MODE=false"
    fi
fi

echo ""

# ── 2. Docker 和 Docker Compose 检查 ──
echo "📋 [2/8] 检查 Docker 环境..."
if command -v docker &>/dev/null; then
    check_pass "Docker 已安装"
else
    check_fail "Docker 未安装"
fi

if command -v docker compose &>/dev/null || docker-compose --version &>/dev/null; then
    check_pass "Docker Compose 已安装"
else
    check_fail "Docker Compose 未安装"
fi

echo ""

# ── 3. Python 依赖检查 ──
echo "📋 [3/8] 检查 Python 依赖..."
if python3 -c "import fastapi" 2>/dev/null; then
    check_pass "FastAPI 已安装"
else
    check_fail "FastAPI 未安装，请运行: pip install -r requirements.txt"
fi

if python3 -c "import langgraph" 2>/dev/null; then
    check_pass "LangGraph 已安装"
else
    check_fail "LangGraph 未安装"
fi

if python3 -c "import qdrant_client" 2>/dev/null; then
    check_pass "qdrant-client 已安装"
else
    check_warn "qdrant-client 未安装，RAG 功能将不可用"
fi

echo ""

# ── 4. 数据库迁移检查 ──
echo "📋 [4/8] 检查数据库迁移状态..."
if command -v alembic &>/dev/null; then
    if alembic current 2>&1 | grep -q "HEAD"; then
        check_pass "数据库已迁移至最新版本"
    else
        check_warn "数据库可能需要迁移，请运行: make db-upgrade"
    fi
else
    check_fail "Alembic 未安装"
fi

echo ""

# ── 5. 测试运行检查 ──
echo "📋 [5/8] 运行单元测试..."
if python3 -m pytest tests/unit/ -q --tb=no 2>&1 | tail -1 | grep -q "passed"; then
    check_pass "单元测试通过"
else
    check_fail "单元测试失败"
fi

echo ""

# ── 6. 端口占用检查 ──
echo "📋 [6/8] 检查端口占用情况..."
for port in 8000 6379 5432; do
    if lsof -i :$port &>/dev/null; then
        check_warn "端口 $port 已被占用"
    else
        check_pass "端口 $port 可用"
    fi
done

echo ""

# ── 7. 磁盘空间检查 ──
echo "📋 [7/8] 检查磁盘空间..."
AVAILABLE_SPACE=$(df -h / | awk 'NR==2 {print $4}')
echo "   可用空间: $AVAILABLE_SPACE"
check_pass "磁盘空间检查完成"

echo ""

# ── 8. 日志目录检查 ──
echo "📋 [8/8] 检查日志和数据目录..."
for dir in logs data backups chat_sessions; do
    if [ -d "$dir" ]; then
        check_pass "目录 $dir 存在"
    else
        mkdir -p "$dir" && check_pass "目录 $dir 已创建"
    fi
done

echo ""
echo "============================================"
echo "  检查结果汇总"
echo "============================================"
echo "✅ 通过: $PASS_COUNT"
echo "❌ 失败: $FAIL_COUNT"
echo "⚠️  警告: $WARN_COUNT"
echo ""

if [ $FAIL_COUNT -gt 0 ]; then
    echo "🚨 发现 $FAIL_COUNT 个严重问题，请先修复后再部署！"
    exit 1
elif [ $WARN_COUNT -gt 0 ]; then
    echo "⚠️  发现 $WARN_COUNT 个警告，建议在生产部署前处理"
    read -p "是否继续部署？(y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "继续部署..."
        exit 0
    else
        echo "部署已取消"
        exit 1
    fi
else
    echo "🎉 所有检查通过，可以安全部署！"
    exit 0
fi