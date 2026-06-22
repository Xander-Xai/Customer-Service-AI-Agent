# 角色审计报告 — 数据守卫（Data Guardian）

> **审计日期**: 2026-06-15
> **审计范围**: db/、alembic/、core/session/、auth/、api/routes/
> **发现总数**: 25 个
> **总体评分**: 7.5/10

---

## 一、Schema 设计

### D-1: users 表缺少 email 字段

**严重程度**: 🟡 P2
**位置**: `db/models.py:33-52`

users 表仅有 username，无 email 字段。对于客服系统，email 是用户找回密码、接收订单通知的关键渠道。当前设计限制了用户账户恢复能力。

**修复建议**: 添加 `email = Column(String(255), unique=True, nullable=True, index=True)`，并补充验证逻辑。

### D-2: users.is_active 使用 Integer 而非 Boolean

**严重程度**: 🟡 P2
**位置**: `db/models.py:47`

`is_active = Column(Integer, default=1)` 使用 Integer 表示布尔状态。SQLAlchemy 的 `Boolean` 类型在 PostgreSQL 中映射为 `BOOLEAN`，语义更清晰，且支持 CHECK 约束。

**修复建议**: 改为 `Column(Boolean, default=True, nullable=False)`，迁移脚本中使用 `server_default="true"`。

### D-3: chat_histories.messages 为 JSON 无长度限制

**严重程度**: 🟡 P2
**位置**: `db/models.py:62`

messages 字段存储完整对话历史（JSON），无大小限制。单条消息可能包含大段 LLM 输出，长期积累后单条记录可达 MB 级，影响查询性能。

**修复建议**: 考虑分表存储（messages 独立表），或在应用层限制单会话消息数（已有 `window_size` 但仅影响内存，不影响持久化）。

### D-4: 缺少 feedbacks → chat_histories 的外键关联

**严重程度**: 🟡 P2
**位置**: `db/models.py:92-103`

feedbacks 表有 `session_id` 和 `message_index`，但无外键指向 chat_histories。这导致：
- 会话删除后反馈记录成为"孤儿数据"
- 无法通过 ORM 级联删除

**修复建议**: 添加 `chat_history_id = Column(Integer, ForeignKey("chat_histories.id"), nullable=True)`，或建立复合外键 `(session_id, message_index)`。

### D-5: audit_logs 缺少索引优化

**严重程度**: 🟢 P3
**位置**: `db/models.py:75-89`

audit_logs 仅有 `user_id` 和 `action` 两个单列索引。高频查询场景（如"查询某时间段内某用户的登录记录"）需要复合索引。

**修复建议**: 添加复合索引 `Index("ix_audit_logs_user_action_time", "user_id", "action", "timestamp")`。

### D-6: prompt_versions 缺少唯一约束

**严重程度**: 🟡 P2
**位置**: `db/models.py:105-119`

`(agent_name, version)` 组合应唯一，但当前仅有 `agent_name` 单列索引。同一 agent 可能出现重复 version，导致 A/B 测试数据混乱。

**修复建议**: 添加 `UniqueConstraint("agent_name", "version")`。

### D-7: 无软删除机制

**严重程度**: 🟡 P2
**位置**: 全局

所有表均无 `deleted_at` 字段。用户删除会话、管理员删除用户等操作将物理删除数据，无法恢复，也不利于审计追踪。

**修复建议**: 为关键表（users, chat_histories）添加 `deleted_at = Column(DateTime(timezone=True), nullable=True)`，查询时自动过滤 `deleted_at IS NULL`。

### D-8: 无 CHECK 约束

**严重程度**: 🟢 P3
**位置**: 全局

`users.role` 仅有注释说明取值范围（customer/agent/supervisor/admin），无数据库级 CHECK 约束。`feedbacks.rating` 仅有注释说明 1/-1，无约束。

**修复建议**:
```python
# users.role
CheckConstraint("role IN ('customer', 'agent', 'supervisor', 'admin')")

# feedbacks.rating
CheckConstraint("rating IN (-1, 1)")
```

---

## 二、迁移安全

### M-1: 003 迁移使用 TO_TIMESTAMP 可能丢失精度

**严重程度**: 🟡 P2
**位置**: `alembic/versions/003_timestamps_to_datetime.py:34`

`postgresql_using="TO_TIMESTAMP(created_at)"` 将 Float 转为 DateTime。原 Float 值可能是 Unix 时间戳（秒级），TO_TIMESTAMP 接收秒级参数是正确的。但如果原值包含毫秒部分（如 `1717603200.123`），小数部分会被截断。

**修复建议**: 确认原 Float 字段是否存储毫秒。若是，应使用 `TO_TIMESTAMP(created_at)` 本身支持小数秒，无需修改；但需在迁移前备份数据。

### M-2: 003 迁移 downgrade 无数据保护

**严重程度**: 🟠 P1
**位置**: `alembic/versions/003_timestamps_to_datetime.py:87-104`

downgrade 将 DateTime 转回 Float，使用 `EXTRACT(EPOCH FROM column)`。此操作不可逆：时区信息丢失，且 Float 精度可能不足以表示毫秒。

**修复建议**: downgrade 前添加数据备份提示，或标记为不可逆迁移（`downgrade = None`），强制手动处理。

### M-3: 迁移脚本缺少事务包裹

**严重程度**: 🟡 P2
**位置**: `alembic/versions/001_initial_schema.py:23-75`

upgrade() 中创建多个表和索引，但 Alembic 默认每个 `op.*` 调用在自动事务中执行。PostgreSQL 的 `CREATE TABLE` 和 `CREATE INDEX` 在事务中可回滚，但 SQLite 不支持事务性 DDL。

**修复建议**: 在 PostgreSQL 环境下确保迁移原子性；SQLite 环境下添加前置检查，避免半完成状态。

### M-4: 002 迁移添加 nullable Boolean

**严重程度**: 🟢 P3
**位置**: `alembic/versions/002_add_missing_tables.py:28-31`

`force_password_change` 定义为 `sa.Boolean(), server_default="0", nullable=True`。nullable=True 允许 NULL 值，但 server_default="0" 确保新记录有默认值。现有记录在迁移后可能为 NULL，与业务语义（应为 False）不一致。

**修复建议**: 迁移后执行数据修复：`UPDATE users SET force_password_change = 0 WHERE force_password_change IS NULL`。

### M-5: init_db 回退到 create_all 可能跳过迁移

**严重程度**: 🟡 P2
**位置**: `db/database.py:72-115`

当 alembic 迁移失败时，自动回退到 `Base.metadata.create_all()`。这会导致：
- 新环境可能从未执行迁移，后续增量迁移无法应用
- 数据库版本与 alembic 版本表不一致

**修复建议**: 生产环境应强制使用 alembic，失败时抛出异常而非静默回退。

---

## 三、数据一致性

### C-1: auth/service.py 多处手动事务无统一封装

**严重程度**: 🟡 P2
**位置**: `auth/service.py:391-414`, `auth/router.py:127`, `api/routes/prompts.py:114`

多处代码手动管理 `db.commit()` / `db.rollback()` / `db.close()`，模式重复但无统一封装。`register_user()` 有 try/except/finally，但 `authenticate_user()` 仅有 try/finally（无 rollback 路径）。

**修复建议**: 使用上下文管理器统一封装：
```python
@contextmanager
def db_transaction():
    db = get_db_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```

### C-2: feedback 路由中 db 会话未正确关闭（异常路径）

**严重程度**: 🟠 P1
**位置**: `api/routes/feedback.py:50-71`

```python
db = get_db_session()
try:
    feedback = Feedback(...)
    db.add(feedback)
    db.commit()
except Exception as e:
    db.rollback()      # 如果异常发生在 db.add 之前，rollback 可能无效
    logger.warning(...)
finally:
    db.close()
```

如果 `db.add()` 之前发生异常（如模型初始化失败），`db.rollback()` 在空会话上调用是安全的，但如果 `db` 本身创建失败，会导致 NameError。

**修复建议**: 使用统一的上下文管理器。

### C-3: PromptManager 缓存与数据库不一致

**严重程度**: 🟡 P2
**位置**: `core/prompt_manager.py:56-77`

PromptManager 使用内存缓存（60秒 TTL），但 `record_feedback()` 直接更新数据库，不刷新缓存。如果缓存命中期间有反馈更新，应用层看到的 `score_avg` 和 `feedback_count` 是旧值。

**修复建议**: 更新数据库后使缓存失效，或缩短 TTL。

### C-4: 会话内存缓存与 Redis/文件后端不一致

**严重程度**: 🟠 P1
**位置**: `core/session/session_manager.py:180-347`

`self.sessions` 是内存字典，Redis 和文件后端是持久化存储。存在以下一致性问题：
1. `create_session()` 先从 Redis 加载，但加载后数据仅在内存中更新，Redis 中的旧数据不会被清理
2. `add_message()` 先更新内存，再异步保存到文件/Redis。如果保存失败，内存与持久化数据不一致
3. `_evict_idle_sessions()` 仅清理内存，不清理 Redis/文件中的数据

**修复建议**: 实现读写穿透（write-through）或定期同步机制。

### C-5: auth/router.py 审计日志提交后无回滚保护

**严重程度**: 🟢 P3
**位置**: `auth/router.py:126-127`

```python
db.add(log)
db.commit()
```

审计日志写入后如果后续业务逻辑失败，审计日志已提交无法回滚。虽然审计日志通常不应回滚，但需要确保业务主事务与审计日志事务分离。

**修复建议**: 使用独立的数据库连接写入审计日志，或采用异步消息队列（已有 MessageBus，但未用于审计日志）。

---

## 四、数据生命周期

### L-1: 无数据备份策略

**严重程度**: 🟠 P1
**位置**: 全局

项目中无任何数据库备份脚本或策略。SQLite 文件位于 `./data/csai.db`，无定时备份；PostgreSQL 无 WAL 归档或 pg_dump 配置。

**修复建议**: 添加备份脚本（SQLite 文件复制 / PostgreSQL pg_dump），并配置 cron 定时任务。

### L-2: 无数据保留策略

**严重程度**: 🟡 P2
**位置**: 全局

audit_logs、chat_histories、feedbacks 表无自动清理机制。长期运行后数据量膨胀，影响性能。

**修复建议**:
- audit_logs：保留 90 天，超期归档或删除
- chat_histories：保留 180 天，超期归档
- feedbacks：永久保留（数据量小）

### L-3: 无 GDPR 合规机制

**严重程度**: 🟡 P2
**位置**: 全局

无用户数据导出、删除（right to erasure）机制。`api/routes/sessions.py` 的 `delete_session` 仅删除内存/Redis 会话，不删除数据库中的 chat_histories 和 feedbacks。

**修复建议**: 实现 `/api/gdpr/export` 和 `/api/gdpr/delete` 端点，支持用户导出和删除个人数据。

### L-4: 会话文件存储无清理

**严重程度**: 🟢 P3
**位置**: `core/session/session_manager.py:240-250`

`_save_to_file()` 将会话保存到 `./chat_sessions/{session_id}.json`，但 `_delete_session_unlocked()` 仅删除内存和 Redis，不删除文件。文件将永久积累。

**修复建议**: 在 `_delete_session_unlocked()` 中添加文件删除逻辑：
```python
if self.storage_backend == "file":
    fp = os.path.join(self.storage_config["storage_dir"], f"{session_id}.json")
    if os.path.exists(fp):
        os.remove(fp)
```

---

## 五、Session 存储安全性

### S-1: 文件会话存储无加密

**严重程度**: 🟠 P1
**位置**: `core/session/session_manager.py:240-250`

`_save_to_file()` 将会话消息以明文 JSON 写入磁盘。若包含敏感信息（用户 PI、订单详情），文件系统泄露将导致数据暴露。

**修复建议**:
1. 使用 SQLite 替代文件存储（已有数据库连接）
2. 或加密后写入文件（Fernet / AES）

### S-2: Redis 会话数据无加密

**严重程度**: 🟡 P2
**位置**: `core/session/session_manager.py:471-491`

Redis 中存储的会话消息为明文 JSON。Redis 通常部署在内网，但如果 Redis 被攻破，会话内容直接暴露。

**修复建议**: 对敏感字段加密后存储，或使用 Redis 的 ACL 限制访问。

### S-3: SESSION_TOKEN_SECRET 弱密钥检测不完整

**严重程度**: 🟡 P2
**位置**: `core/session/session_manager.py:354-369`

`_get_token_secret()` 检查了几个占位符值，但未检查常见弱密钥（如 "secret", "password", "12345678"）。

**修复建议**: 扩展弱密钥黑名单，并强制要求最小长度（如 32 字符）。

### S-4: 会话 ID 验证后仍可能路径遍历

**严重程度**: 🟡 P2
**位置**: `core/session/session_manager.py:66`, `core/session/session_manager.py:230-244`

虽然 `_SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9\-_]{1,64}$")` 过滤了非法字符，但 `_save_to_file()` 直接使用 `session_id` 作为文件名。如果未来放宽正则（如允许 `/`），将导致路径遍历。

**修复建议**: 文件名生成时使用 `hashlib.sha256(session_id.encode()).hexdigest()[:16] + ".json"`，或添加额外路径安全检查。

### S-5: Redis 连接 URL 可能泄露密码到日志

**严重程度**: 🟡 P2
**位置**: `core/session/session_manager.py:216-217`

```python
safe_url = url.split("@")[-1] if "@" in url else url
logger.info(f"Redis 连接成功: {safe_url}")
```

如果 URL 格式为 `redis://:password@host:port`，`safe_url` 为 `host:port`，密码被正确隐藏。但如果 URL 包含用户名（`redis://user:password@host`），`split("@")` 后仍可能暴露用户名。

**修复建议**: 使用 `urlparse` 提取 host:port，而非字符串分割。

---

## 六、汇总表格

| 编号 | 严重程度 | 类别 | 问题 | 位置 | 影响 | 修复建议 |
|------|----------|------|------|------|------|----------|
| D-1 | 🟡 P2 | Schema | users 表缺少 email 字段 | `db/models.py:33` | 无法发送通知、找回密码 | 添加 email 字段 |
| D-2 | 🟡 P2 | Schema | is_active 使用 Integer 而非 Boolean | `db/models.py:47` | 语义不清，无 CHECK 约束 | 改为 Boolean |
| D-3 | 🟡 P2 | Schema | messages JSON 无大小限制 | `db/models.py:62` | 单记录过大影响性能 | 分表或限制大小 |
| D-4 | 🟡 P2 | Schema | feedbacks 无外键关联 | `db/models.py:92` | 孤儿数据风险 | 添加外键约束 |
| D-5 | 🟢 P3 | Schema | audit_logs 缺少复合索引 | `db/models.py:75` | 查询性能差 | 添加复合索引 |
| D-6 | 🟡 P2 | Schema | prompt_versions 缺少唯一约束 | `db/models.py:105` | 重复 version | 添加 UniqueConstraint |
| D-7 | 🟡 P2 | Schema | 无软删除机制 | 全局 | 数据无法恢复 | 添加 deleted_at 字段 |
| D-8 | 🟢 P3 | Schema | 无 CHECK 约束 | 全局 | 非法值可入库 | 添加 CHECK 约束 |
| M-1 | 🟡 P2 | 迁移 | TO_TIMESTAMP 可能丢失精度 | `alembic/003:34` | 时间精度损失 | 确认并备份 |
| M-2 | 🟠 P1 | 迁移 | downgrade 不可逆 | `alembic/003:87` | 数据丢失 | 标记不可逆或备份 |
| M-3 | 🟡 P2 | 迁移 | 缺少事务包裹 | `alembic/001:23` | 半完成状态 | 确保原子性 |
| M-4 | 🟢 P3 | 迁移 | nullable Boolean | `alembic/002:28` | NULL 语义不一致 | 数据修复 |
| M-5 | 🟡 P2 | 迁移 | init_db 回退 create_all | `db/database.py:72` | 版本不一致 | 生产环境禁用回退 |
| C-1 | 🟡 P2 | 一致性 | 手动事务无统一封装 | `auth/service.py:391` | 代码重复、易遗漏 | 统一上下文管理器 |
| C-2 | 🟠 P1 | 一致性 | feedback db 未正确关闭 | `api/routes/feedback.py:50` | 连接泄漏 | 使用上下文管理器 |
| C-3 | 🟡 P2 | 一致性 | PromptManager 缓存不一致 | `core/prompt_manager.py:56` | 数据过时 | 更新后失效缓存 |
| C-4 | 🟠 P1 | 一致性 | 内存与持久化不一致 | `core/session/session_manager.py:180` | 数据丢失 | 实现写穿透 |
| C-5 | 🟢 P3 | 一致性 | 审计日志无独立事务 | `auth/router.py:126` | 无法回滚业务 | 使用独立连接 |
| L-1 | 🟠 P1 | 生命周期 | 无备份策略 | 全局 | 数据丢失风险 | 添加备份脚本 |
| L-2 | 🟡 P2 | 生命周期 | 无数据保留策略 | 全局 | 数据膨胀 | 添加自动清理 |
| L-3 | 🟡 P2 | 生命周期 | 无 GDPR 机制 | 全局 | 合规风险 | 实现导出/删除 |
| L-4 | 🟢 P3 | 生命周期 | 会话文件未清理 | `core/session/session_manager.py:240` | 磁盘泄漏 | 删除时清理文件 |
| S-1 | 🟠 P1 | Session | 文件会话明文存储 | `core/session/session_manager.py:240` | 数据泄露 | 加密或改用 DB |
| S-2 | 🟡 P2 | Session | Redis 会话明文存储 | `core/session/session_manager.py:471` | 中间人攻击 | 加密敏感字段 |
| S-3 | 🟡 P2 | Session | 弱密钥检测不完整 | `core/session/session_manager.py:354` | 会话劫持 | 扩展黑名单 |
| S-4 | 🟡 P2 | Session | 路径遍历风险 | `core/session/session_manager.py:230` | 文件系统攻击 | 哈希文件名 |
| S-5 | 🟡 P2 | Session | Redis URL 日志泄露 | `core/session/session_manager.py:216` | 凭据泄露 | 使用 urlparse |

---

## 统计

| 严重程度 | 数量 |
|----------|------|
| 🔴 P0 | 0 |
| 🟠 P1 | 5 |
| 🟡 P2 | 15 |
| 🟢 P3 | 5 |
| **总计** | **25** |

## 优先级建议

1. **立即修复（P1）**: C-2（连接泄漏）、C-4（内存持久化不一致）、M-2（迁移不可逆）、L-1（无备份）、S-1（明文存储）
2. **短期修复（P2）**: Schema 完善（D-1~D-8）、一致性封装（C-1, C-3）、GDPR（L-3）
3. **长期优化（P3）**: 索引优化（D-5）、审计事务分离（C-5）、文件清理（L-4）
