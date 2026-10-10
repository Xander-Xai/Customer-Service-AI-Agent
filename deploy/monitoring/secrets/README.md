# Prometheus 抓取凭据目录（挂载到容器 `/etc/prometheus/secrets`）

这里放的是 **Prometheus 抓取本服务指标用的凭据文件**，不是应用的业务数据。

## 用法

```bash
# 1. 在 .env 里设置 MONITORING_ADMIN_TOKEN（不是占位符）
# 2. 生成抓取凭据文件
make monitoring-token

# 3. 启动监控栈（prometheus + grafana + alertmanager + loki + promtail）
make monitoring-up
```

`make monitoring-token` 会把 `MONITORING_ADMIN_TOKEN` 原样写入
`deploy/monitoring/secrets/monitoring_admin_token`（权限 0600）。

## 为什么是 fail-closed 的

`monitoring/prometheus.yml` 用的是 Prometheus 的标准 `bearer_token_file`：

```yaml
bearer_token_file: '/etc/prometheus/secrets/monitoring_admin_token'
```

**该文件不存在时 Prometheus 拒绝启动**（配置加载失败），而不是静默地把 target
标成 down 之后什么都不采。这是刻意的：指标断链最危险的形式就是「看起来在跑、
其实什么都没收到」—— DLQ 告警正是这样被无声地废掉的。

应用侧接受两种等价形式（`api/utils.py::check_admin_token`）：

- `Authorization: Bearer <token>` —— Prometheus `bearer_token_file` 发出的形式；
- `X-Admin-Token: <token>` —— curl / 浏览器调试用。

## 边界（不得越界宣称）

- 本仓库**没有**提交任何真实凭据；本目录只提交 `.gitkeep` 与本文档。
- `make monitoring-token` 只做本地文件生成，**不**验证 Prometheus 真的抓到了数据。
  「告警闭环已验证」需要真的制造一次 dead-letter 并在 Alertmanager 看到通知。