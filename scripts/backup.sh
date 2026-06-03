#!/usr/bin/env bash
# ============================================================
# 客服 AI Agent — 数据备份脚本
# 用法: ./scripts/backup.sh [目标目录]
# 建议通过 cron 定时执行: 0 2 * * * /path/to/scripts/backup.sh /data/backups
# ============================================================
set -euo pipefail

BACKUP_ROOT="${1:-./backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}"
MAX_BACKUPS=7  # 保留最近 N 份备份

mkdir -p "${BACKUP_DIR}"

echo "[$(date)] 开始备份 → ${BACKUP_DIR}"

# ── 1. ChromaDB 知识库备份 ──
CHROMA_DIR="./chroma_db"
if [ -d "${CHROMA_DIR}" ]; then
    echo "  [ChromaDB] 备份 ${CHROMA_DIR}"
    tar -czf "${BACKUP_DIR}/chroma_db.tar.gz" -C "$(dirname ${CHROMA_DIR})" "$(basename ${CHROMA_DIR})"
    echo "  [ChromaDB] 完成"
else
    echo "  [ChromaDB] 目录不存在，跳过"
fi

# ── 2. Redis 快照备份 ──
REDIS_URL="${REDIS_URL:-redis://localhost:6379}"
# 从 REDIS_URL 中提取 host:port
REDIS_HOST=$(echo "${REDIS_URL}" | sed -E 's|redis://([^:/]+)(:[0-9]+)?|\1|')
REDIS_PORT=$(echo "${REDIS_URL}" | sed -E 's|redis://[^:/]+:([0-9]+)|\1|')
[ -z "${REDIS_PORT}" ] && REDIS_PORT=6379

if command -v redis-cli &>/dev/null; then
    echo "  [Redis] 触发 BGSAVE"
    redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" BGSAVE 2>/dev/null || echo "  [Redis] BGSAVE 失败（Redis 可能未运行）"
    sleep 2
    # 复制 RDB 文件
    REDIS_DATA_DIR=$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" CONFIG GET dir 2>/dev/null | tail -1 || echo "")
    REDIS_DBFILE=$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" CONFIG GET dbfilename 2>/dev/null | tail -1 || echo "dump.rdb")
    if [ -n "${REDIS_DATA_DIR}" ] && [ -f "${REDIS_DATA_DIR}/${REDIS_DBFILE}" ]; then
        cp "${REDIS_DATA_DIR}/${REDIS_DBFILE}" "${BACKUP_DIR}/redis_dump.rdb"
        echo "  [Redis] 完成"
    else
        echo "  [Redis] RDB 文件未找到，跳过"
    fi
else
    echo "  [Redis] redis-cli 未安装，跳过"
fi

# ── 3. 环境配置备份（不含密钥） ──
if [ -f ".env" ]; then
    # 移除敏感字段后备份
    grep -v -E '^(API_KEY|ADMIN_TOKEN|OPENAI_API_KEY|ERP_APP_SECRET)=' ".env" > "${BACKUP_DIR}/env.safe" 2>/dev/null || true
    echo "  [Config] .env（脱敏）已备份"
fi

# ── 4. 备份轮转（保留最近 N 份） ──
echo "  [轮转] 清理旧备份（保留最近 ${MAX_BACKUPS} 份）"
ls -1dt "${BACKUP_ROOT}"/*/ 2>/dev/null | tail -n +$((MAX_BACKUPS + 1)) | xargs rm -rf 2>/dev/null || true

echo "[$(date)] 备份完成 → ${BACKUP_DIR}"
ls -lh "${BACKUP_DIR}/"
