#!/usr/bin/env bash
# ===== Fresh-DB migration gate: empty PostgreSQL -> alembic upgrade head -> schema =====
#
# 在空白 database 上跑完整 Alembic chain（base -> 001 -> 002 -> 003 ->
# 8ea0ec90ba74 -> 004_distributed_agent_runtime），验证业务核心表 + 运行时表均存在。
# 不使用 alembic stamp，不使用 Base.metadata.create_all()。
#
# 用法：
#   scripts/verify_fresh_db_migration.sh
#   TEST_FRESH_DB_ADMIN_URL=postgresql://user:pass@host:5432/postgres \
#       scripts/verify_fresh_db_migration.sh

set -euo pipefail

ADMIN_URL="${TEST_FRESH_DB_ADMIN_URL:-postgresql://postgres:postgres@localhost:5432/postgres}"

cd "$(dirname "$0")/.."

echo "[fresh-db] admin=$ADMIN_URL"
FRESH_DB_ADMIN_URL="$ADMIN_URL" python3 -m tests.integration.fresh_db_migrate
