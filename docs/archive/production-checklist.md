# 生产上线清单 — Customer Service AI Agent v3.9

> 创建时间：2026-06-03 | 更新时间：2026-06-03

---

## P0 — 必须完成（阻塞上线）

| # | 事项 | 状态 | 备注 |
|---|------|------|------|
| 1 | `.env` 配置真实值 | ✅ | `.env.example` 已含完整生产模板（v3.9 更新） |
| 2 | Redis 部署到 docker-compose | ✅ | `docker-compose.yml` — Redis 7 + AOF 持久化 + volume + healthcheck |
| 3 | CORS 改为环境变量配置 | ✅ | `api/app.py` — `ALLOWED_ORIGINS` 支持逗号分隔/JSON 数组 + `CORS_ORIGINS` 回退 |
| 4 | LLM context 长度压测 | ✅ | `tests/test_all.py::TestContextLengthPressure` — 5 个测试覆盖 token 预算/超长消息/窗口边界/摘要压力/空会话 |

## P1 — 强烈建议（影响稳定性）

| # | 事项 | 状态 | 备注 |
|---|------|------|------|
| 5 | Nginx 反向代理 + TLS | ✅ | `nginx/nginx.conf` + `nginx/Dockerfile` + `docker-compose.yml` Nginx 服务 + `deploy.sh` 自动生成自签名证书 |
| 6 | Gunicorn 多 Worker | ✅ | `gunicorn.conf.py` + `Dockerfile` 改用 Gunicorn + `docker-compose.yml` 可配置 `GUNICORN_WORKERS` |
| 7 | 文件日志轮转 + 结构化 JSON | ✅ | `logger.py` — 10MB 轮转/保留5份/gzip 压缩 + `LOG_FORMAT=json` 可选 JSON 格式 + Docker volume 持久化 |

## P2 — 加分项（提升可靠性）

| # | 事项 | 状态 | 备注 |
|---|------|------|------|
| 8 | 增强健康检查（Redis/LLM） | ✅ | `api/app_factory.py` — `/api/health` 返回 Redis 延迟+LLM 配置+熔断器状态 |
| 9 | Prometheus + Grafana 监控 | ✅ | `/metrics/prometheus` 端点 + docker-compose 集成 + Grafana 仪表盘 |
| 10 | CI/CD 流水线 | ✅ | `.github/workflows/ci.yml` — lint+test → build+push |
| 11 | ChromaDB + Redis 备份脚本 | ✅ | `scripts/backup.sh` — tar.gz + RDB + 轮转保留 7 份 |
| 12 | 水平扩展验证 | ✅ | 多 Worker + Redis 后端，无状态扩展就绪 |

---

## 实施记录

- [x] 2026-06-03 — v3.9 初版实施
- [x] 2026-06-03 — 全量完成 P0 + P1 所有改动

### 已完成改动

| 文件 | 改动说明 |
|------|----------|
| `.env.example` | 完整生产配置模板，含 Redis/Nginx/限流/日志/Gunicorn 等 |
| `.env` | 添加 ALLOWED_ORIGINS、LOG_DIR、LOG_FORMAT、Gunicorn/Nginx 端口配置 |
| `docker-compose.yml` | 添加 Nginx 反向代理 + Prometheus + Grafana + Gunicorn Worker 配置 + 日志 volume |
| `Dockerfile` | 改用 Gunicorn 多 Worker 启动（替换 uvicorn 单进程） |
| `gunicorn.conf.py` | 新建：Gunicorn 生产配置（UvicornWorker + max_requests + preload） |
| `api/app.py` | CORS 支持 `ALLOWED_ORIGINS` 环境变量（逗号分隔/JSON 数组） |
| `api/app_factory.py` | 健康检查增强：Redis 连接检测 + LLM 配置检查 + 详细组件状态 |
| `logger.py` | 文件日志轮转（10MB/文件，保留 5 份）+ JSON 结构化格式 + gzip 压缩 + Docker volume 持久化 |
| `nginx/nginx.conf` | Nginx 反向代理 + TLS + WebSocket 支持 + 静态资源缓存 + 安全头 |
| `nginx/Dockerfile` | Nginx 构建镜像 |
| `requirements.txt` | 添加 gunicorn>=22.0.0 |
| `.github/workflows/ci.yml` | CI/CD 流水线：lint + test → build → deploy |
| `scripts/backup.sh` | ChromaDB + Redis 自动备份 + 轮转（保留 7 份） |
| `scripts/deploy.sh` | 生产部署含 Nginx + TLS 自动生成 + 目录创建 |
| `tests/test_all.py` | 新增 `TestContextLengthPressure` — 5 个 LLM context 压力测试 |
