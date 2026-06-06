# 生产完善方案（v4.0 Production Hardening）

> 目标：将当前技术原型完善为可接入真实数据的生产级系统
> 日期：2026-06-05

---

## 一、用户认证体系

### 1.1 数据库层
- SQLite + SQLAlchemy ORM（轻量级，无需额外服务）
- User 表：id / username / password_hash / role(admin/user) / created_at
- ChatHistory 表：id / user_id / session_id / messages(JSON) / created_at

### 1.2 认证 API
- POST /api/auth/register — 注册（密码 bcrypt 哈希）
- POST /api/auth/login — 登录（返回 JWT token）
- GET /api/auth/me — 获取当前用户信息

### 1.3 中间件改造
- API Key 认证保持（系统间调用）
- 新增 JWT Bearer 认证（终端用户）
- 两种认证方式并存，互不干扰

---

## 二、ERP 数据同步

### 2.1 RAG 自动同步
- 启动时从 ERP 拉取产品数据 → 写入 ChromaDB
- 新增同步脚本：scripts/sync_erp_to_rag.py
- 增量更新机制（基于修改时间戳）

### 2.2 知识库管理 API
- GET /api/knowledge — 查看知识库统计
- POST /api/knowledge/seed — 手动触发种子数据
- DELETE /api/knowledge/{collection}/{id} — 删除文档

---

## 三、前端增强

### 3.1 用户界面
- 登录/注册页面
- 用户头像和角色显示
- 历史会话持久化展示

### 3.2 管理后台
- 知识库管理（查看/添加/删除文档）
- 用户管理（查看用户列表、角色设置）
- 系统配置可视化

---

## 四、告警通知

### 4.1 通知渠道
- Webhook 通用接口（兼容钉钉/企业微信/飞书）
- 邮件通知（SMTP）
- 告警规则配置

### 4.2 告警管理 API
- GET /api/alerts/config — 查看告警配置
- PUT /api/alerts/config — 更新告警配置
- POST /api/alerts/test — 测试告警通知

---

## 五、数据库持久化

### 5.1 会话持久化
- SQLite 存储会话消息（替代纯内存）
- 支持历史会话查询和导出
- 定期清理过期数据

### 5.2 操作日志
- AuditLog 表：记录关键操作（登录/查询/修改）
- 支持按时间/用户/操作类型查询

---

## 实施优先级

| 阶段 | 内容 | 预计工作量 |
|------|------|-----------|
| P0 | 用户认证 + 数据库 | 核心 |
| P1 | ERP→RAG 同步 + 知识库 API | 重要 |
| P1 | 前端登录 + 管理页面 | 重要 |
| P2 | 告警通知 + Webhook | 增强 |
| P2 | 会话持久化 + 审计日志 | 增强 |

---

## 文件变更清单

### 新增文件
```
db/
  __init__.py
  models.py          # SQLAlchemy 模型
  database.py        # 数据库连接和初始化
  
auth/
  __init__.py
  router.py          # 认证路由 (register/login/me)
  service.py         # 认证业务逻辑
  middleware.py       # JWT 中间件
  
knowledge/
  __init__.py
  router.py          # 知识库管理路由
  sync.py            # ERP→RAG 同步服务
  
alerts/
  __init__.py
  router.py          # 告警配置路由
  notifier.py        # 通知发送（Webhook/Email）
  
templates/
  login.html         # 登录页面
  admin.html         # 管理后台
  
scripts/
  sync_erp_to_rag.py  # 数据同步脚本
  init_db.py          # 数据库初始化脚本
```

### 修改文件
```
requirements.txt       # 新增依赖
config.py             # 新增认证/数据库配置
api/app.py            # 认证中间件改造
api/app_factory.py    # 数据库初始化
```
