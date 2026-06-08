# 生产上线问题清单与修复记录

> 生成日期: 2026-06-06 | 状态: 修复中

---

## 阻塞项（BLOCKING）— 必须修复才能上线

### B1. PostgreSQL 驱动未安装
- **文件**: `requirements.txt:35`
- **问题**: `psycopg2-binary` 被注释，PostgreSQL 容器启动后应用无法连接
- **修复**: 取消注释 `psycopg2-binary>=2.9.9`

### B2. 生产 DATABASE_URL 为空
- **文件**: `.env.prod:136`
- **问题**: `DATABASE_URL=` 空值，应用回退到 SQLite，PostgreSQL 形同虚设
- **修复**: 设置 `DATABASE_URL=postgresql://csai:CHANGE_ME@postgres:5432/csai`

### B3. PostgreSQL 默认密码太弱
- **文件**: `docker-compose.yml:102`
- **问题**: 默认密码 `csai_password_change_me`，生产环境存在安全风险
- **修复**: `.env.prod` 中设置强密码并确保 docker-compose 读取

### B4. 无 CD 流水线
- **文件**: `.github/workflows/ci.yml`
- **问题**: CI 仅运行测试，无 Docker 构建/推送/部署步骤
- **修复**: 新增 `build-and-push` 和 `deploy` jobs

### B5. `.env.dev` 含真实 API Key
- **文件**: `.env.dev:8`
- **问题**: 包含硅基流动真实 Key `sk-tnwwg...`，若曾提交 git 历史需轮换
- **修复**: 替换为占位符；文档提醒检查 git 历史

---

## 高优先级（HIGH）— 上线第一周内

### H1. 备份脚本缺少 PostgreSQL 备份
- **文件**: `scripts/backup.sh`
- **问题**: 仅备份 ChromaDB 和 Redis，不备份数据库
- **修复**: 新增 `pg_dump` 备份步骤

### H2. Docker 镜像版本未固定
- **文件**: `docker-compose.yml:135,152`
- **问题**: `prom/prometheus:latest`、`grafana/grafana:latest` 不可复现
- **修复**: 固定为 `prom/prometheus:v2.51.0`、`grafana/grafana:10.4.1`

### H3. 部署过程有停机
- **文件**: `scripts/deploy.sh`
- **问题**: 先 `docker compose down` 再 `up`，部署期间服务不可用
- **修复**: 改为先构建再重启，加入健康检查验证

### H4. Grafana 默认密码为 admin
- **文件**: `docker-compose.yml:158`, `.env.prod:129`
- **问题**: 默认 `admin` 密码
- **修复**: `.env.prod` 中设为 `CHANGE_ME` 占位

### H5. Makefile 缺少运维命令
- **文件**: `Makefile`
- **问题**: 无 db-migrate、backup、canary、scale 命令
- **修复**: 补充全部运维 target

---

## 中优先级（MEDIUM）— 上线一个月内

### M1. 时间戳用 Float 而非 DateTime
- **文件**: `db/models.py`
- **问题**: 所有时间字段用 `Float` 存储 Unix 时间戳，难以处理时区
- **建议**: 迁移为 `DateTime(timezone=True)` + Alembic migration

### M2. 无依赖锁定文件
- **文件**: `requirements.txt`
- **问题**: 无 `requirements-lock.txt`，构建不可复现
- **修复**: 生成 `requirements-lock.txt`

### M3. CSP 允许 unsafe-inline
- **文件**: `api/app.py`
- **问题**: `script-src 'self' 'unsafe-inline'` 削弱 XSS 防护
- **建议**: 前端改用 nonce 脚本

### M4. Loki 未集成到主 compose
- **文件**: `docker-compose.monitoring.yml`（独立文件）
- **问题**: 生产部署命令不包含 Loki，日志不聚合
- **修复**: 生产部署脚本中加入 `--profile monitoring`

### M5. 无 Prometheus Alertmanager
- **文件**: `monitoring/`
- **问题**: 告警规则有但缺 Alertmanager 路由服务
- **建议**: 添加 Alertmanager 容器 + 配置

### M6. OpenTelemetry 包未声明
- **文件**: `requirements.txt`
- **问题**: `core/tracing.py` 依赖的 OTEL 包不在依赖列表
- **修复**: 添加为可选依赖

---

## 修复记录

| # | 问题 | 状态 | 修复内容 |
|---|------|------|----------|
| B1 | PostgreSQL 驱动 | ✅ 已修复 | `requirements.txt` 取消 psycopg2-binary 注释 |
| B2 | DATABASE_URL 为空 | ✅ 已修复 | `.env.prod` 设置 PostgreSQL 连接字符串 |
| B3 | PG 默认密码 | ✅ 已修复 | docker-compose 改为 `${POSTGRES_PASSWORD:?...}` 强制设置 |
| B4 | 无 CD 流水线 | ✅ 已修复 | CI 新增 `build-and-push` + `deploy` jobs（GHCR + SSH） |
| B5 | .env.dev 真实 Key | ✅ 已修复 | 替换为 `your_siliconflow_api_key_here` 占位符 |
| H1 | PG 备份缺失 | ✅ 已修复 | `scripts/backup.sh` 新增 pg_dump 步骤 |
| H2 | Docker 版本未固定 | ✅ 已修复 | Prometheus v2.51.0、Grafana 10.4.1 |
| H3 | 部署停机 | ✅ 已修复 | `deploy.sh` 改为先构建→滚动重启→健康检查验证 |
| H4 | Grafana 默认密码 | ✅ 已修复 | docker-compose 改为 `${GRAFANA_PASSWORD:?...}` + .env.example 占位 |
| H5 | Makefile 运维命令 | ✅ 已修复 | 新增 12 个 target（db-migrate/backup/canary/scale 等） |
| M1 | Float 时间戳 | ✅ 已修复 | 迁移为 DateTime(timezone=True) + Alembic 003 + auth 适配 |
| M2 | 依赖锁定 | ✅ 已修复 | 生成 `requirements-lock.txt`（186 包）+ `make lock` target |
| M3 | CSP unsafe-inline | ✅ 已修复 | 改为 nonce 脚本，每个请求生成唯一 nonce |
| M4 | Loki 集成 | ✅ 已修复 | 集成到 docker-compose.prod.yml，生产部署自动启用 |
| M5 | Alertmanager | ✅ 已修复 | 新增容器 + 配置 + Prometheus alerting 集成 |
| M6 | OTEL 依赖 | ✅ 已修复 | `requirements.txt` 添加可选依赖注释 |
