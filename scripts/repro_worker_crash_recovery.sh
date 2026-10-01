#!/usr/bin/env bash
# ===== Distributed Runtime: worker crash -> redelivery -> recovery 复现脚本 =====
#
# 真实组件：Redis broker（Celery acks_late + visibility_timeout）+ PostgreSQL
# AgentRun 真相源 + 两个独立 Celery worker 进程 + deterministic fake runtime。
#
# 流程：enqueue -> Worker A 进入 RUNNING -> SIGKILL A -> 未 ACK 任务经 visibility
# timeout 重新可见 -> Worker B 接管过期 lease -> SUCCEEDED。
#
# 用法：
#   scripts/repro_worker_crash_recovery.sh
#   TEST_DISTRIBUTED_DB_URL=postgresql://... TEST_REDIS_URL=redis://... \
#       scripts/repro_worker_crash_recovery.sh
#
# 该脚本只是 tests/integration/test_worker_crash_recovery.py 的薄封装；
# 测试默认因缺少真实 PG/Redis 而 skip，此脚本显式提供连接后运行。

set -euo pipefail

DB_URL="${TEST_DISTRIBUTED_DB_URL:-postgresql://postgres:postgres@localhost:5432/cosmetics_ai}"
REDIS_URL="${TEST_REDIS_URL:-redis://localhost:6379}"

cd "$(dirname "$0")/.."

echo "[repro] DB=$DB_URL"
echo "[repro] REDIS=$REDIS_URL"
echo "[repro] running worker crash recovery integration test..."

TEST_DISTRIBUTED_DB_URL="$DB_URL" \
TEST_REDIS_URL="$REDIS_URL" \
  python3 -m pytest tests/integration/test_worker_crash_recovery.py \
    -q -o addopts="" -p no:cacheprovider --no-header
