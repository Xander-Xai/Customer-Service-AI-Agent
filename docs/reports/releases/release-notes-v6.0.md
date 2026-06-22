# v6.0 发布说明 — Qdrant 向量数据库迁移

**发布日期**：2026-06-20

## 概述

本版本将核心 RAG 知识库从 ChromaDB 迁移到 Qdrant，实现了更高的并发性能、独立部署能力和生产环境可靠性。同时提供平滑迁移路径，支持并行运行模式，确保零宕机切换。

## 新功能

### Qdrant 向量数据库

- **完全替代 ChromaDB**：Qdrant（Rust 编写）作为独立 Docker 容器部署，支持 REST + gRPC 双协议
- **高并发**：Qdrant 原生支持高并发读写，适合多 Agent 并行检索场景
- **余弦距离检索**：与 bge-small-zh-v1.5 embedding 模型原生匹配，`_parse_query_result` 自动兼容转换为 L2 距离
- **连接探活**：初始化时自动检查 Qdrant 可用性，失败时 graceful degradation

### 数据迁移脚本

- `scripts/migrate_chroma_to_qdrant.py`：一键将 ChromaDB 持久化数据迁移到 Qdrant
- 支持 4 个 collection 全量迁移（product_knowledge / faq / tech_support / complaint_knowledge）

### 并行运行模式

- `VECTOR_DB_MODE=parallel`：同时运行 ChromaDB + Qdrant，适用于灰度切换
- `VECTOR_DB_MODE=chroma_legacy`：保持旧模式（默认，兼容现有部署）
- `VECTOR_DB_MODE=qdrant_only`：纯 Qdrant 模式（生产推荐）

### 兼容 API 层

- `rag/knowledge_base.py` 保持向后兼容，无缝重定向到 `QdrantKnowledgeBase`
- `KnowledgeBaseProtocol` 接口不变，调用方（Agent / Graph / 路由）零改动
- embedding 计算保留在应用侧（bge-small-zh-v1.5），不依赖 Qdrant 内置

## 文件变更

| 操作 | 文件 | 说明 |
|------|------|------|
| 新建 | `rag/qdrant_knowledge_base.py` | Qdrant 知识库核心实现 |
| 新建 | `rag/legacy_chroma.py` | ChromaDB 遗留兼容层 |
| 新建 | `scripts/migrate_chroma_to_qdrant.py` | 数据迁移脚本 |
| 新建 | `tests/unit/test_qdrant_knowledge_base.py` | Qdrant 单元测试（~15 tests） |
| 新建 | `tests/unit/test_migration_compat.py` | 兼容性测试（10+ tests） |
| 修改 | `core/config.py` | 添加 Qdrant 配置项 |
| 修改 | `core/container.py` | 知识库初始化选择 |
| 修改 | `api/routes/monitoring.py` | 健康检查改用 Qdrant |
| 修改 | `deploy/compose/docker-compose.yml` | 添加 Qdrant 服务 |
| 修改 | `deploy/compose/docker-compose.prod.yml` | 添加 Qdrant 持久卷 |
| 修改 | `Makefile` | 添加 Qdrant 运维命令 |
| 修改 | `requirements.txt` | 添加 qdrant-client 依赖 |
| 修改 | `tests/e2e/test_all.py` | 移除 ChromaDB 全局状态清理 |

## 配置变更

```env
# ===== v6.0: Qdrant 向量数据库配置 =====
QDRANT_HOST=localhost
QDRANT_PORT=6333
QDRANT_GRPC_PORT=6334
VECTOR_DB_MODE=chroma_legacy  # parallel | qdrant_only | chroma_legacy
```

## 升级指南

### 新部署

```bash
# 1. 启动 Qdrant
docker compose -f deploy/compose/docker-compose.yml up -d qdrant

# 2. 设置环境变量
export VECTOR_DB_MODE=qdrant_only
export QDRANT_HOST=localhost
export QDRANT_PORT=6333

# 3. 启动应用
make dev
```

### 从 ChromaDB 迁移

```bash
# 1. 启动 Qdrant
make qdrant-start

# 2. 运行迁移脚本
python3 scripts/migrate_chroma_to_qdrant.py --chroma-dir ./chroma_db

# 3. 切换到 Qdrant
export VECTOR_DB_MODE=qdrant_only
make dev
```

## 回退方案

若 Qdrant 连接失败，`CosmeticsKnowledgeBase.available` 自动为 False，检索降级返回空结果。可设置 `VECTOR_DB_MODE=chroma_legacy` 回退到 ChromaDB。

## 依赖变更

- 新增：`qdrant-client>=1.12.0`
- 新增：`sentence-transformers>=2.2.0`（原为 ChromaDB 间接依赖）
- 移除：`chromadb>=0.5.0`（保留在 legacy 模式中可选安装）
