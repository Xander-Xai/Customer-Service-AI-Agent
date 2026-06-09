# 生产上线修复设计方案

**日期**: 2026-06-07
**版本**: v4.3.0-hotfix
**状态**: 已批准

## 背景

生产上线验收审查发现 4 项需修复的问题（1 High Bug + 1 Medium Bug + 2 安全加固），需在部署前全部解决。

## 修复清单

### Fix 1: Feedback.created_at 类型修复 [P0]

- **文件**: `api/app.py:1195`
- **问题**: `created_at=time.time()` 传入 float，但模型定义为 `DateTime(timezone=True)`
- **方案**: 改为 `datetime.now(timezone.utc)`，并在文件顶部添加 `from datetime import datetime, timezone` import
- **验证**: 反馈提交不抛异常

### Fix 2: async create_session 调用修复 [P1]

- **文件**: `core/container.py:192`
- **问题**: `create_session` 是异步方法但被同步调用（缺少 await）
- **方案**: 改为 `await _session_mgr.create_session(session_id)`
- **验证**: 测试中 RuntimeWarning 消失

### Fix 3: 认证端点限流 + Prometheus 端点认证 [P1]

- **文件**: `api/app.py`
- **问题 A**: `/api/auth/refresh`、`/api/auth/logout` 等子路径无限流保护
- **方案 A**: 在 `rate_limit_middleware` 中为 auth 子路径添加统一限流（登录已有 5/5min，refresh/logout 使用通用限流即可）
- **问题 B**: `/metrics/prometheus` 未在 auth_middleware 的 admin 保护列表中
- **方案 B**: 将 `/metrics/prometheus` 加入 `required_auth = "admin"` 路径列表
- **验证**: 未认证请求 Prometheus 端点返回 401

### Fix 4: 生产安全配置生成 [P0]

- **文件**: `.env.prod`
- **问题**: 所有密钥为 `CHANGE_ME_*` 占位符
- **方案**: 编写 `scripts/generate_prod_env.py` 脚本，使用 `secrets` 模块生成密码学安全的随机密钥，自动替换所有占位符
- **生成的密钥**: JWT_SECRET, SESSION_TOKEN_SECRET, API_KEY, MONITORING_ADMIN_TOKEN, REDIS_PASSWORD, POSTGRES_PASSWORD, GRAFANA_PASSWORD
- **验证**: 生成的 `.env.prod` 中无 CHANGE_ME 占位符，密钥长度 >= 32 字符

## 执行策略

- 4 个 Agent 并行执行（文件已分离，无冲突）
- Fix 1 + Fix 3 合并为同一个 Agent（同一文件 api/app.py）
- Fix 2 独立 Agent（core/container.py）
- Fix 4 独立 Agent（scripts/ + .env.prod）
- 全部完成后运行测试套件验证

## 验收标准

1. `pytest tests/ --ignore=tests/test_e2e_real_llm.py` 全部通过
2. 无 RuntimeWarning（async 相关）
3. `.env.prod` 无 CHANGE_ME 占位符
4. `/metrics/prometheus` 未认证返回 401
