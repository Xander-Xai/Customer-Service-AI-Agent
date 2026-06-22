# 🚀 客服 AI Agent 上线前快速检查清单

> **预计耗时**: 30-60分钟  
> **适用场景**: 生产环境首次部署或重大版本升级

---

## ✅ 必做项（Blocking）

### 1. 环境配置 [10分钟]

```bash
# 1.1 生成生产配置
python3 scripts/generate_prod_env.py
cp .env.prod.generated .env.prod

# 1.2 编辑配置文件
vim .env.prod
```

**必须修改的配置**:
- [ ] `OPENAI_API_KEY` — 填入真实的LLM API密钥
- [ ] `API_KEY` — 生成强随机密钥: `openssl rand -hex 32`
- [ ] `JWT_SECRET` — 生成强随机密钥（≥32字符）
- [ ] `SESSION_TOKEN_SECRET` — 生成强随机密钥
- [ ] `REDIS_PASSWORD` — 设置Redis密码
- [ ] `POSTGRES_PASSWORD` — 设置数据库密码
- [ ] `GRAFANA_PASSWORD` — 设置Grafana管理员密码
- [ ] `CORS_ORIGINS` — 设置为实际域名（不要使用*）
- [ ] `DEV_MODE=false` — 确保不是true
- [ ] `QDRANT_HOST` / `QDRANT_PORT` — 确认 Qdrant 连接配置
- [ ] `VECTOR_DB_MODE=qdrant_only` — 生产环境修改为 `qdrant_only`

### 2. 预部署检查 [5分钟]

```bash
# 运行自动化检查脚本
chmod +x scripts/pre_deploy_check.sh
./scripts/pre_deploy_check.sh
```

**期望结果**: 
- ✅ 所有检查通过
- ⚠️ 如有警告，确认可以接受
- ❌ 如有失败，必须修复后继续

### 3. 构建与测试 [15分钟]

```bash
# 3.1 运行单元测试
make test

# 3.2 构建生产镜像
make prod-build

# 3.3 本地验证（可选但推荐）
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml up -d
curl http://localhost:8000/api/health
docker compose down
```

**期望结果**:
- ✅ 测试全部通过（1,361+ 项，覆盖率 ≥80%）
- ✅ Docker镜像构建成功
- ✅ 健康检查返回200

### 4. 存储准备 [5分钟]

```bash
# 4.1 执行数据库迁移
make db-upgrade

# 4.2 验证迁移状态
alembic current

# 4.3 验证 Qdrant 可访问
curl -s http://localhost:6333/collections | jq '.result'
```

**期望结果**:
- ✅ 显示当前迁移版本为HEAD
- ✅ Qdrant 返回集合列表（默认创建 product_knowledge / faq / tech_support / complaint_knowledge）

### 5. 数据目录准备 [2分钟]

```bash
# 创建必要目录
mkdir -p data logs backups chat_sessions
chmod 755 data logs backups chat_sessions
```

---

## ✅ 推荐项（Recommended）

### 6. 监控配置 [10分钟]

```bash
# 6.1 配置告警通知渠道
# 在 .env.prod 中设置:
# ALERT_WEBHOOKS='[{"name":"钉钉","url":"https://oapi.dingtalk.com/robot/send?access_token=xxx","type":"dingtalk"}]'

# 6.2 启动监控栈（可选）
make monitoring-up
```

**验证**:
- [ ] Grafana可访问: http://localhost:3000
- [ ] Prometheus可访问: http://localhost:9090
- [ ] Loki可访问: http://localhost:3100

### 7. 备份配置 [5分钟]

```bash
# 7.1 执行首次备份
./scripts/backup.sh ./backups

# 7.2 配置定时备份（crontab）
# 0 2 * * * /path/to/scripts/backup.sh /data/backups >> /var/log/backup.log 2>&1
```

**验证**:
- [ ] 备份文件已生成
- [ ] 备份文件大小合理

### 8. 安全加固 [5分钟]

```bash
# 8.1 检查防火墙规则
sudo ufw status

# 8.2 仅开放必要端口
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS
sudo ufw allow 22/tcp    # SSH
sudo ufw enable
```

**注意**: 根据实际需求调整端口

---

## 🚀 执行部署

### 方式一: Make命令（推荐）

```bash
# 一键部署
make prod

# 验证部署
make prod-ps
curl http://localhost:8000/api/health
```

### 方式二: Docker Compose手动部署

```bash
# 启动服务
docker compose -f deploy/compose/docker-compose.yml \
               -f deploy/compose/docker-compose.prod.yml \
               up -d --build

# 查看日志
docker compose logs -f app

# 等待服务就绪（约30-60秒）
sleep 60

# 健康检查
curl http://localhost:8000/api/health
```

---

## ✅ 部署后验证

### 9. 功能验证 [10分钟]

```bash
# 9.1 健康检查
curl http://localhost:8000/api/health | jq

# 9.2 测试聊天接口
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query": "你好", "session_id": "test-session"}' | jq

# 9.3 检查日志无错误
docker compose logs app | grep -i error | tail -5

# 9.4 验证监控指标
curl http://localhost:8000/api/metrics | head -20
```

**期望结果**:
- ✅ 健康检查返回正常状态
- ✅ 聊天接口返回有效响应
- ✅ 日志无ERROR级别错误
- ✅ 指标端点返回Prometheus格式数据

### 10. 性能基线 [5分钟]

```bash
# 记录初始性能指标
echo "=== 资源使用 ==="
docker stats --no-stream

echo "=== 响应时间 ==="
time curl -s http://localhost:8000/api/health > /dev/null

echo "=== 缓存状态 ==="
docker compose exec redis redis-cli INFO stats | grep hits
```

**记录这些数据用于后续对比**

---

## 📊 上线后监控（前72小时）

### 每小时检查
- [ ] 错误率 < 1%
- [ ] P95延迟 < 10s
- [ ] CPU使用率 < 80%
- [ ] 内存使用率 < 85%
- [ ] 磁盘空间充足

### 每天检查
- [ ] 用户反馈评分趋势
- [ ] LLM API调用成本
- [ ] 缓存命中率
- [ ] 活跃会话数
- [ ] 告警通知是否正常

---

## 🐛 常见问题速查

### 问题1: 服务启动失败
```bash
# 检查日志
docker compose logs app

# 常见原因:
# - 端口被占用: lsof -i :8000
# - 环境变量缺失: docker compose config | grep -A5 environment
# - 依赖服务未就绪: docker compose ps
```

### 问题2: LLM API调用失败
```bash
# 验证API Key
echo $OPENAI_API_KEY | wc -c

# 测试连通性
curl -H "Authorization: Bearer $OPENAI_API_KEY" \
     https://api.siliconflow.cn/v1/models
```

### 问题3: 数据库连接失败
```bash
# 检查PostgreSQL状态
docker compose logs postgres

# 验证连接字符串
echo $DATABASE_URL

# 测试连接
docker compose exec postgres psql -U csai -d csai -c "SELECT 1;"
```

### 问题4: Redis连接失败
```bash
# 检查Redis状态
docker compose logs redis

# 测试连接
docker compose exec redis redis-cli ping
```

---

## 📞 紧急回滚

如果上线后发现严重问题：

```bash
# 1. 立即停止服务
make prod-down

# 2. 清理容器
docker compose -f deploy/compose/docker-compose.yml \
               -f deploy/compose/docker-compose.prod.yml \
               down -v

# 3. 恢复上一版本代码
git checkout PREVIOUS_TAG

# 4. 重新部署
make prod

# 5. 验证恢复
curl http://localhost:8000/api/health
```

---

## ✅ 上线成功标志

完成以下所有项即表示上线成功：

- [ ] 服务正常运行超过24小时
- [ ] 无ERROR级别日志
- [ ] 用户可正常使用聊天功能
- [ ] 监控指标在正常范围
- [ ] 告警系统正常工作
- [ ] 备份自动执行成功
- [ ] 性能满足SLA要求

---

## 📝 检查清单签署

| 项目 | 执行人 | 完成时间 | 备注 |
|------|--------|----------|------|
| 环境配置 | ________ | ____-__-__ __:__ | |
| 预部署检查 | ________ | ____-__-__ __:__ | |
| 构建与测试 | ________ | ____-__-__ __:__ | |
| 数据库准备 | ________ | ____-__-__ __:__ | |
| 执行部署 | ________ | ____-__-__ __:__ | |
| 功能验证 | ________ | ____-__-__ __:__ | |
| 监控配置 | ________ | ____-__-__ __:__ | |

**最终确认**: □ 可以上线  □ 需要延期

**确认人**: ________________  
**日期**: ____-__-__

---

**祝上线顺利！🎉**

如需帮助，请参考:
- 📖 [完整运维手册](../operations/production-operations-guide.md)
- ✅ [生产准备度检查清单](production-readiness-checklist.md)