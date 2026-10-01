# Kubernetes 最小可验证部署

> 目的：证明这套 Agent 系统**不存在必须依赖单个 Python 进程内状态才能工作**的设计。
> 不是建设完整云原生平台。API 与 Celery worker 独立 Deployment、独立扩缩。

## 1. 组件与状态边界

| 组件 | 部署 | 状态来源 |
|------|------|---------|
| API（FastAPI/gunicorn） | `api-deployment.yaml`（2+ 副本） | 无本地持久化 |
| Worker（Celery） | `worker-deployment.yaml`（2+ 副本，独立） | 无本地持久化 |
| LangGraph Checkpoint | — | PostgreSQL |
| AgentRun / 审批 / 副作用幂等 | — | PostgreSQL |
| Session / broker / thread-lock / event stream | — | Redis |
| 向量检索 | — | Qdrant |

API Pod 与 Worker Pod **都不在本地磁盘保存 checkpoint / session / AgentRun / result**；
Pod 可随时删除、滚动、重启。禁止在同一个 Pod 内同时启动 API + Celery worker。

## 2. 前置

- Kubernetes 1.25+，`kubectl` 可用，已安装 nginx ingress controller 与 metrics-server
  （HPA 需要）。
- 镜像：把各 Deployment 的 `image:` 换成你的 registry 镜像（如
  `ghcr.io/<org>/customer-ai-agent:6.3`）。
- PostgreSQL / Redis / Qdrant：
  - 生产：使用 managed / external service，改 ConfigMap 的 `QDRANT_HOST` 与 Secret 的
    `DATABASE_URL` / `REDIS_URL`；
  - 演示：`kubectl apply -f deploy/k8s/demo/dependencies.yaml`（单副本 emptyDir，
    Pod 重启丢数据，仅演示）。

## 3. 部署

```bash
# 1) Secret（只能放占位符，绝不能提交真实 key）
cp deploy/k8s/secret.example.yaml deploy/k8s/secret.yaml
# 编辑 secret.yaml：替换所有 REPLACE_ME_* / CHANGE_ME_*（secret.yaml 已 gitignore）
kubectl apply -f deploy/k8s/secret.yaml

# 2) 演示依赖（可选，仅演示环境）
kubectl apply -f deploy/k8s/demo/dependencies.yaml

# 3) 应用（Namespace/ConfigMap/API/Worker/Service/Ingress/HPA）
kubectl apply -k deploy/k8s/

# 或逐个文件：
kubectl apply -f deploy/k8s/namespace.yaml -f deploy/k8s/configmap.yaml \
  -f deploy/k8s/api-deployment.yaml -f deploy/k8s/api-service.yaml \
  -f deploy/k8s/worker-deployment.yaml -f deploy/k8s/ingress.yaml \
  -f deploy/k8s/hpa-api.yaml -f deploy/k8s/hpa-worker.yaml
```

> 未替换占位符时，`core/config.py` 生产校验会 fail fast（拒绝 `sk-placeholder` /
> `change-me` 等），API/worker 不会带假密钥启动。

## 4. 滚动 / 扩缩 / 回滚

```bash
# 状态
kubectl -n customer-ai get deploy,po,svc,ingress,hpa

# 滚动更新（改镜像后）
kubectl -n customer-ai set image deploy/api api=<registry>/customer-ai-agent:6.4
kubectl -n customer-ai rollout status deploy/api
kubectl -n customer-ai rollout status deploy/worker

# 手动扩缩（HPA 之外）
kubectl -n customer-ai scale deploy/api --replicas=3
kubectl -n customer-ai scale deploy/worker --replicas=4

# 回滚
kubectl -n customer-ai rollout undo deploy/api
kubectl -n customer-ai rollout undo deploy/worker

# 日志
kubectl -n customer-ai logs -l app=api --tail=200 -f
kubectl -n customer-ai logs -l app=worker --tail=200 -f
```

## 5. 健康检查

```bash
# 探针路径（无认证）
kubectl -n customer-ai exec deploy/api -- curl -sf http://localhost:8000/api/health | python -m json.tool
# 关注：status / database.connected / redis.connected / qdrant.connected /
#       langgraph_checkpoint.status
kubectl -n customer-ai exec deploy/worker -- \
  celery -A runtime.celery_app:celery_app inspect ping
```

- API：readiness/liveness = `GET /api/health`。
- Worker：readiness/liveness = `celery inspect ping`（无 HTTP 端口）。

## 6. SSE / WebSocket

Ingress（nginx）已设置：

- `proxy-buffering: "off"`、`proxy-read-timeout/send-timeout: 3600`（SSE 长连接）；
- `proxy-http-version: "1.1"` + `websocket-services: api`（`/ws/chat` 升级）。

SSE 端点 `GET /api/runs/{id}/stream` 支持 `Last-Event-ID` 断线续读；事件来自 Redis
Stream，Run 最终状态来自 PostgreSQL——因此跨 Pod 重连、API 重启都不丢。

## 7. 扩缩策略

- API：`hpa-api.yaml` 基于 CPU / memory（标准指标，需 metrics-server）。
- Worker：`hpa-worker.yaml` 先用 CPU 作为可部署基线。**最理想依据是队列深度 /
  pending runs**（worker 多为 IO 等待，CPU 常偏低）。本 PR 不引入 KEDA /
  prometheus-adapter；基于队列深度的 custom-metric autoscaling 是**后续项**。
  仓库已有 `agent_run_queue_wait_seconds` / `agent_run_total{status}` /
  `agent_worker_active` 等指标可作起点，但尚缺「队列深度」指标。

## 8. 故障验证（证明无单进程状态依赖）

```bash
# 场景 1：删除 API Pod，正在执行的 run 继续（执行在 worker）
kubectl -n customer-ai delete pod -l app=api
# 另开终端观察：run 状态仍从 RUNNING -> SUCCEEDED，GET /api/runs/{id} 可读结果

# 场景 2：删除 worker Pod，任务被重投递且遵守幂等
kubectl -n customer-ai delete pod -l app=worker
# Celery acks_late + reject_on_worker_lost 触发重投递；
# AgentRun 从 PostgreSQL 恢复；Tool idempotency 防重复副作用。

# 场景 3：滚动重启 API 与 worker（验证 checkpoint / session 跨进程）
kubectl -n customer-ai rollout restart deploy/api deploy/worker
```

预期：run 不丢、不重复副作用、SSE 可 `Last-Event-ID` 续读、最终结果从 PostgreSQL 读取。

## 9. 调试

```bash
kubectl -n customer-ai describe pod -l app=api
kubectl -n customer-ai get events --sort-by=.lastTimestamp | tail -30
kubectl -n customer-ai get hpa -w
kubectl -n customer-ai top pod
```

常见问题：

- Pod `CrashLoopBackOff`：多为生产配置校验失败（占位符密钥 / 缺 `DATABASE_URL` /
  CORS 为空 / checkpoint backend 非 postgres）。看 `kubectl logs` 的 `配置校验失败`。
- Worker 不消费：检查 `REDIS_URL` / broker 连通、`AGENT_RUN_DISPATCH=celery`、
  队列名 `AGENT_RUN_QUEUE=agent_runs`。
- HPA 不工作：确认 metrics-server 与资源 requests 已设置。

## 10. 边界（刻意不引入）

本部署**不包含**：service mesh / Istio / ArgoCD / Kafka / Temporal / Operator。
目的只是最小可验证的 API/Worker 独立扩缩与状态外置；更重的平台化能力按需再评估。
