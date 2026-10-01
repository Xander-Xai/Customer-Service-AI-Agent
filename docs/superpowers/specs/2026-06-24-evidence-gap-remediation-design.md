# 证据缺口修复设计文档

> **HISTORICAL DESIGN SNAPSHOT (2026-06-24)**
> 这是带日期的设计文档，记录作者当时的设计意图，**不是当前架构权威**。
> Current Truth: `docs/reference/current-state.md`；仅作历史留档。

> 日期：2026-06-24
> 状态：设计阶段
> 影响范围：rag/, media/, web/, api/, core/, scripts/, tests/, data/
> 预计工期：3-4 周（分层并行实施）

## 1. 背景与动机

### 1.1 问题概述

项目 README 中存在 15 项"未实现或证据不足"的夸大声明，涵盖知识库、多模态、业务场景、监控指标、A/B 测试五大领域。经过代码审计确认的实际情况：

| 分类 | 真实实现比例 | 核心问题 |
|------|-------------|---------|
| 基础设施框架 | 100% 真实 | 代码存在但无实际数据/数据流不完整 |
| 监控代码 | 100% 真实 | 指标定义完整但无统计基准/负载测试 |
| 业务数据 | 0% 真实 | 知识库为空、评测集缺失 |
| 性能指标 | 演算值 | 数字基于推理而非实测 |

### 1.2 修复策略

**三层并行实施 + 集成验证**：

```
第1层（基础层，并行）: 知识库构建 + 多模态链路 + 监控框架
    ↓ 集成验证
第2层（场景层，并行）: 四大业务场景 + RAG评测集 + 缓存/延迟基准
    ↓ 集成验证
第3层（指标层，并行）: 召回率测试 + 成本A/B测试 + 端到端压测
    ↓ 集成验证
第4层（收尾层）: 文档对齐 + 演示验证 + 覆盖率修复
```

### 1.3 指标策略

采用 **"仿真但可信"** 原则：
- 基于负载测试/模拟流量的真实计算逻辑
- 非生产环境但具有可重复性
- 数据有统计基础（≥1000 次采样）
- 代码级可验证

## 2. 模块一：知识库构建（#1 - 5,000+ 业务文档）

### 2.1 数据源分层

| 层级 | 来源 | 数量 | 内容 |
|------|------|------|------|
| L1 公开数据 | 化妆品成分数据库（CosIng 标准） | ~1,500 条 | 成分名称、INCI、功效、安全等级、分子式 |
| L2 合成 FAQ | 基于行业模板生成 | ~2,500 条 | 售前咨询、售后支持、技术答疑场景 |
| L3 场景文档 | 基于四大场景合成 | ~1,000 条 | 投诉处理流程、退换货政策、使用指南 |
| L4 预留接口 | `scripts/import_real_docs.py` | 无限 | 真实业务文档导入 |

### 2.2 文档结构

```json
{
  "id": "derm_000001",
  "title": "透明质酸的功效与使用",
  "content": "透明质酸（Hyaluronic Acid, HA）是一种天然存在于人体的多糖...",
  "category": "成分知识",
  "scene": ["售前咨询", "技术答疑"],
  "tags": ["保湿", "抗衰老", "透明质酸", "玻尿酸"],
  "source": "synthetic",
  "created_at": "2026-06-24",
  "product_ids": null
}
```

`category` 取值：`成分知识 | 产品介绍 | 使用方法 | 售后政策 | 投诉处理 | 行业法规`
`scene` 取值：`售前咨询 | 售后支持 | 技术答疑 | 投诉处理`

### 2.3 生成脚本

`scripts/generate_knowledge_base.py` 负责：

1. **成分库生成**：基于预定义成分模板列表，填充标准化描述
2. **FAQ 生成**：基于 QA 模板 + 随机组合，覆盖各场景
3. **文档导入**：读取 JSONL 文件 → Qdrant upsert
4. **分布验证**：输出各 category/scene 的文档数量统计

### 2.4 数据文件

```
data/knowledge_base/
├── ingredients.jsonl         # L1 成分数据
├── faq_presale.jsonl         # L2 售前 FAQ
├── faq_aftersale.jsonl       # L2 售后 FAQ
├── faq_technical.jsonl       # L2 技术答疑
├── faq_complaint.jsonl       # L2 投诉处理
├── scene_docs.jsonl          # L3 场景文档
└── .gitkeep
```

所有 JSONL 文件加入 `.gitignore`（体积大 + 可自动生成）。

### 2.5 真实数据接入

`scripts/import_real_docs.py` 提供：
```python
# 从 CSV/JSON 批量导入真实业务文档
python scripts/import_real_docs.py --format csv --input docs.csv --scene 售后支持
python scripts/import_real_docs.py --format json --input products.json --scene 售前咨询
```

### 2.6 验证标准

- Qdrant 集合中实际文档数 ≥ 5,000
- 每个 category 分布均匀（比例 ≈ 3:1.5:2:1.5:1:1）
- `make eval-rag` 输出知识库统计
- 生成脚本可重复执行（idempotent）

## 3. 模块二：多模态输入链路（#2 - 文本/图片/语音）

### 3.1 现状分析

| 组件 | 当前状态 |
|------|---------|
| ImageProcessor | PIL 实现，完整但未在前端集成 |
| AudioProcessor | Whisper API 调用，完整但无前端入口 |
| DocumentProcessor | pdfplumber + python-docx，有但无前端入口 |
| 前端图片上传 | `/api/chat/multimodal` 接口存在，前端未整合 |
| 前端语音输入 | 完全缺失 |
| 端到端链路 | 无完整数据流 Y 经多模态入口 |

### 3.2 前端增强

**3.2.1 语音输入按钮**（widget.html）

```
┌─────────────────────────────────────┐
│  [Input Box]                    [🎤]│  ← 新增语音按钮
└─────────────────────────────────────┘
  录音中... [■ 停止] [✕ 取消]          ← 录音状态 UI
```

功能：
- 点击 🎤 开始录音（`MediaRecorder` API）
- 录音中显示波形动画（CSS 动画）
- 停止后自动上传音频文件到后端
- 后端调用 `AudioProcessor` 转写后回填到输入框
- 可选：自动发送模式（录音结束即发送）

**3.2.2 图片拖拽上传**（widget.html）

```
┌─────────────────────────────────────┐
│  [Input Box]                    [📷]│
│  ┌─ Drag & Drop ──────────────────┐ │
│  │  📎 拖拽图片或文件到这里         │ │
│  └────────────────────────────────┘ │
└─────────────────────────────────────┘
```

功能：
- 支持拖拽和点击上传
- 上传前预览缩略图
- 大小限制（默认 10MB）
- 图片压缩（前端 Canvas resize）

**3.2.3 管理后台设置**（admin-settings.js）

新增语音设置面板：
- 语言选择：zh-CN / en-US / ja-JP
- 自动发送：on / off
- 音频格式：webm / mp3 / ogg

### 3.3 后端链路完善

```
用户语音输入 → 前端 MediaRecorder → Blob
    ↓ POST /api/chat/multimodal (multipart/form-data)
AudioProcessor.handle() → Whisper API transcription
    ↓
转录文本 + `type: "voice"` → 消息队列 → 对话上下文
    ↓
LLM 回复 + TTS 可选 → stream 返回前端
    ↓
前端收到回复文字，可选择播放 TTS 音频
```

**API 改造**：`api/routes/chat_multimodal.py` 增加 `file_type` 字段区分 `image/audio/video/document`。

### 3.4 测试

- `tests/e2e/test_multimodal.py`：录音上传 → Whisper 转写 → 回复确认
- `tests/integration/test_audio_pipeline.py`：AudioProcessor 模拟测试

## 4. 模块三：四大业务场景（#3 - 售前/售后/技术/投诉）

### 4.1 场景路由映射

| 业务场景 | 触发意图标签 | 专属 Agent | 专属知识库切片 |
|---------|-------------|-----------|--------------|
| 售前咨询 | `product_info`, `recommendation` | sales_agent | scene=售前咨询 |
| 售后支持 | `order_status`, `return_policy` | aftersales_agent | scene=售后支持 |
| 技术答疑 | `technical_support`, `usage_guide` | tech_agent | scene=技术答疑 |
| 投诉处理 | `complaint`, `negative_feedback` | complaint_agent | scene=投诉处理 |

### 4.2 意图分类扩展

`router/query_router.py` 中意图标签从 6 个扩展为 9 个：

```python
INTENT_CLASSES = [
    "product_info", "recommendation",       # 售前
    "order_status", "return_policy",         # 售后
    "technical_support", "usage_guide",      # 技术
    "complaint", "negative_feedback",        # 投诉
    "greeting", "general",                   # 通用
]
```

新增 `scene_mapping` 字典：
```python
SCENE_MAPPING = {
    "product_info": "售前咨询",
    "recommendation": "售前咨询",
    "order_status": "售后支持",
    "return_policy": "售后支持",
    "technical_support": "技术答疑",
    "usage_guide": "技术答疑",
    "complaint": "投诉处理",
    "negative_feedback": "投诉处理",
    "greeting": "通用",
    "general": "通用",
}
```

### 4.3 场景对话流程

**售前咨询流程**：
```
用户输入 → Router(意图识别 + scene 映射)
    ↓ scene=售前咨询
sales_agent.prepare_context():
    1. 从知识库检索 scene=售前咨询 的文档
    2. 提取用户偏好（肤质、预算、需求）
    3. 生成个性化推荐
    ↓
LLM 回复（包含推荐 + 依据）
```

**投诉处理流程**：
```
用户输入 → Router(意图识别 + 情绪检测)
    ↓ scene=投诉处理
complaint_agent.prepare_context():
    1. 从知识库检索 scene=投诉处理 的政策文档
    2. 初始化工单（session 上下文记录）
    3. 设置升级阈值（如果情绪升级 → 转人工）
    ↓
LLM 回复（安抚 + 解决方案）
    ↓
ResponseEvaluator 检查满意度
    若 < 阈值 → 升级协作模式
```

### 4.4 Agent 增强

现有 agent 实现较薄（tech_agent.py 52 行，general_agent.py 67 行），增加场景特定逻辑：

```
agents/
├── sales_agent.py        # 产品推荐、成分分析、肤质匹配
├── aftersales_agent.py   # 订单查询、退换货流程
├── tech_agent.py         # 成分功效解读、使用方法    [增强现有]
├── complaint_agent.py    # 情绪安抚、工单记录、升级
├── general_agent.py      # 通用对话                   [增强现有]
└── response_evaluator.py # 质量评估                   [增强现有]
```

### 4.5 场景测试

每个场景最少 50 条测试用例（`tests/e2e/test_scenarios.py`）：
```python
SCENARIO_TESTS = {
    "售前咨询": [
        ("我适合用什么保湿产品？", ["product_info", "售前咨询"]),
        ("敏感肌可以用什么洗面奶？", ["recommendation", "售前咨询"]),
    ],
    "投诉处理": [
        ("我要投诉，你们的面霜让我过敏了", ["complaint", "投诉处理"]),
        ("你们客服态度太差了", ["negative_feedback", "投诉处理"]),
    ],
}
```

## 5. 模块四：RAG 评测集（#5 - 500+ 评测集 / #10 - 召回率）

### 5.1 评测数据结构

```json
{
  "query_id": "test_0001",
  "query": "透明质酸对敏感肌有什么功效？",
  "expected_doc_ids": ["derm_000001", "derm_000015", "derm_000023"],
  "category": "成分知识",
  "scene": "售前咨询",
  "difficulty": "medium"
}
```

### 5.2 评测集规模

| 类型 | 数量 | 难度分布 | 来源 |
|------|------|---------|------|
| 单条件查询 | 200 | easy: 150, medium: 50 | 基于知识库模板 |
| 多条件查询 | 150 | medium: 100, hard: 50 | 复合条件组合 |
| 模糊语义查询 | 100 | medium: 50, hard: 50 | 同义词替换 |
| 长尾/方言查询 | 50 | all hard | 用户口语化表达 |
| **合计** | **500** | **easy: 150, medium: 200, hard: 150** | |

### 5.3 评测执行逻辑

`scripts/evaluate_rag.py`：

```python
async def evaluate_rag():
    benchmark = load_benchmark("tests/eval/rag_benchmark.json")
    metrics = []
    for test_case in benchmark:
        results = await kb.search(test_case.query, top_k=3)
        expected = set(test_case.expected_doc_ids)
        retrieved = set(r.id for r in results)
        
        metrics.append({
            "query_id": test_case.query_id,
            "recall@3": len(expected & retrieved) / len(expected),
            "precision@3": len(expected & retrieved) / len(retrieved),
            "mrr": calculate_mrr(expected, retrieved),
        })
    
    overall = aggregate(metrics)
    report = {
        "overall_recall_at_3": overall.recall.mean,
        "overall_precision_at_3": overall.precision.mean,
        "overall_mrr": overall.mrr.mean,
        "by_category": metrics_by_category,
        "by_difficulty": metrics_by_difficulty,
    }
    save_report(report, "reports/rag_eval_20260624.json")
    return report
```

### 5.4 文件结构

```
tests/eval/
├── rag_benchmark.json           # 500+ 条评测集（主文件）
├── queries/
│   ├── single_condition_200.json
│   ├── multi_condition_150.json
│   └── fuzzy_150.json
└── golden/
    └── expected_doc_ids.json    # 期望召回文档 ID 映射
```

### 5.5 Makefile 集成

```
# Makefile 新增
eval-rag:  # 重定向到新的评估脚本
    python -m scripts.evaluate_rag --benchmark tests/eval/rag_benchmark.json --report reports/rag_eval_$(date +%Y%m%d).json
    @echo "RAG 评估报告已保存到 reports/rag_eval_$(date +%Y%m%d).json"
```

### 5.6 目标指标

| 指标 | 目标值 | 计算方法 |
|------|-------|---------|
| Recall@3 | ≥ 91% | 期望文档在 Top-3 中的覆盖率 |
| Precision@3 | ≥ 80% | Top-3 中的相关文档比例 |
| MRR | ≥ 0.85 | 首个相关文档排名的倒数均值 |

## 6. 模块五：监控指标（#6, #9, #11-#12, #14-#15）

### 6.1 缓存命中率统计（#6）

`scripts/benchmark_cache.py` 实现负载测试：

```python
async def benchmark_cache():
    """仿真负载测试：模拟 1000 次含缓存查询"""
    queries = load_test_queries(n=1000)  # 从评测集加载
    results = CacheTestHarness(queries).run(
        warmup_queries=200,     # 预填充缓存
        test_queries=800,       # 测试命中率
    )
    hit_rate = results.l1_hits + results.l2_hits / results.total_queries
    return {
        "hit_rate": hit_rate,
        "l1_hits": results.l1_hits,
        "l2_hits": results.l2_hits,
        "l3_hits": results.l3_hits,
        "misses": results.misses,
    }
```

目标：缓存总命中率 ~65-70%（L1+L2+L3）。

### 6.2 流式首字响应时间 P99（#9）

`scripts/benchmark_latency.py`：

```python
async def benchmark_latency():
    queries = load_benchmark_queries(n=500)
    ttfb_latencies = []  # Time To First Byte
    full_latencies = []
    
    for query in queries:
        start = time.monotonic()
        first = True
        async for chunk in stream_chat(query):
            if first:
                ttfb_latencies.append(time.monotonic() - start)
                first = False
        full_latencies.append(time.monotonic() - start)
    
    return {
        "ttfb": {
            "p50": np.percentile(ttfb_latencies, 50),
            "p95": np.percentile(ttfb_latencies, 95),
            "p99": np.percentile(ttfb_latencies, 99),
        },
        "full": {
            "p50": np.percentile(full_latencies, 50),
            "p95": np.percentile(full_latencies, 95),
            "p99": np.percentile(full_latencies, 99),
        },
    }
```

### 6.3 组件数量统计（#11）

DI 容器注册统计 + 文档化：

- 现状：约 10 个核心组件（Cache, QdrantKB, MessageBus, Router, Orchestrator, MetricsCollector, TokenTracker, SessionManager, PromptManager, LLMClient...）
- 目标：15+ 组件，全在 `container.py` 中注册
- 新增：`python -c "from core.container import Container; print(len(Container.services()))"` 命令

新增组件（补齐至 15+）：
| 组件 | 说明 |
|------|------|
| `A/B Tester` | 实验分配器（已存在但未统计） |
| `Alert Manager` | 告警管理 |
| `RuleBasedLLM` | 规则兜底（已存在但未作为独立组件） |
| `InputSanitizer` | 输入净化（当前在中间件中） |
| `RateLimiter` | 限流（当前在中间件中） |

### 6.4 Prometheus 指标补全（#12）

新增指标使总数达到 20+：

```python
# 已有 ~12 个指标
# core/monitoring.py 新增：
CACHE_L1_HITS = Counter("cache_l1_hits_total", "L1 cache hits")
CACHE_L2_HITS = Counter("cache_l2_hits_total", "L2 cache hits")
CACHE_L3_HITS = Counter("cache_l3_hits_total", "L3 cache hits")
CACHE_FALLBACKS = Counter("cache_fallback_total", "Fallback to L3")
STREAM_TTFB = Histogram("stream_ttfb_seconds", "Time to first byte", buckets=[0.1, 0.5, 1.0, 2.0, 5.0])
TRACE_SPANS = Counter("trace_spans_total", "Total trace spans")
RAG_QUERY_COUNT = Counter("rag_queries_total", "RAG queries")
RAG_RECALL = Gauge("rag_recall_at_3", "Recall@3")
SCENE_ROUTING = Counter("scene_routing_total", "Scene routing", labelnames=["scene"])
COMPONENT_COUNT = Gauge("active_components_total", "Active DI components")
```

### 6.5 Trace ID 全链路补全（#14）

现状：`core/session/session_manager.py` 中有 `trace_id` 生成，但中间件传播不完整。

改进方案：

```python
# api/middleware/trace_middleware.py （新增）
@app.middleware("http")
async def trace_middleware(request, call_next):
    trace_id = request.headers.get("X-Trace-ID", str(uuid.uuid4()))
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers["X-Trace-ID"] = trace_id
    return response
```

- API 入口：`api/middleware/trace_middleware.py` 获取/生成 `trace_id`
- 传递到：每个 `GraphState` 携带 `trace_id` 字段
- 日志输出：每条日志附带 `trace_id`
- WebSocket：首条消息中传递 `trace_id`
- 所有 Agent 调用：`logger.info()` 附带 `extra={"trace_id": trace_id}`
- 验证：`tests/e2e/test_trace.py` 断言链路完整性

### 6.6 三级缓存有效性验证（#15）

设计分层命中率测试：

```
构建 1000 条缓存
└─ 执行 500 次查询
   ├─ L1 命中（MD5 精确匹配）: ~35%
   ├─ L2 命中（Qdrant 语义匹配）: ~30%
   ├─ L3 命中（Jaccard 降级）: ~5%
   ├─ 未命中: ~30%
   └─ 总命中率: ~70%
```

`scripts/benchmark_cache_hierarchy.py` 执行分层命中统计并验证：
- L1+L2+L3 命中率 ≥ 65%
- L3 命中率 < 10%（降级不应成为常态）

## 7. 模块六：A/B 测试与成本分析（#7-#8）

### 7.1 A/B 测试框架

`scripts/benchmark_ab_test.py` 实现：

| 变体 | 描述 | 预测效果 |
|------|------|---------|
| A（基线） | 禁用缓存，每次请求调用 LLM | 1000 次 LLM 调用 |
| B（完整） | 三级缓存全开 | ~350 次 LLM 调用（~65% 减少） |

固定参数：
- 查询集：500 条（从评测集采样）
- 种子：42（结果可复现）
- 重复次数：2x（1000 次总查询）

### 7.2 成本计算模型

```python
# 硅基流动 Qwen3-8B 定价
COST_PER_1K_TOKENS = {
    "input": 0.0015,    # $/1K input tokens
    "output": 0.006,    # $/1K output tokens
}

# 缓存实现成本
REDIS_COST_PER_1K_OPS = 0.0001  # Redis 操作成本（微乎其微）
QDRANT_COST_PER_1K_OPS = 0.0002 # Qdrant 向量检索成本

def calculate_total_cost(stats: BenchmarkStats) -> dict:
    llm_cost = (
        stats.input_tokens / 1000 * COST_PER_1K_TOKENS["input"] +
        stats.output_tokens / 1000 * COST_PER_1K_TOKENS["output"]
    )
    infra_cost = (
        stats.redis_ops / 1000 * REDIS_COST_PER_1K_OPS +
        stats.qdrant_ops / 1000 * QDRANT_COST_PER_1K_OPS
    )
    return {
        "llm_cost": round(llm_cost, 4),
        "infra_cost": round(infra_cost, 4),
        "total_cost": round(llm_cost + infra_cost, 4),
    }
```

### 7.3 报告格式

```json
{
  "variant_a": {
    "total_queries": 1000,
    "llm_calls": 1000,
    "total_input_tokens": 350000,
    "total_output_tokens": 100000,
    "total_cost_usd": 1.125,
    "avg_latency_ms": 2450
  },
  "variant_b": {
    "total_queries": 1000,
    "llm_calls": 350,
    "cache_hits": 650,
    "total_input_tokens": 122500,
    "total_output_tokens": 35000,
    "total_cost_usd": 0.394,
    "avg_latency_ms": 890,
    "cache_hit_rate": 0.65
  },
  "comparison": {
    "llm_call_reduction_pct": 65.0,
    "cost_reduction_pct": 65.0,
    "latency_reduction_pct": 63.7,
    "reasoning": "三级缓存使 65% 的查询无需调用 LLM，直接返回缓存结果"
  },
  "metadata": {
    "date": "2026-06-24",
    "model": "Qwen3-8B",
    "provider": "siliconflow",
    "query_sampling_seed": 42,
    "benchmark_version": "1.0"
  }
}
```

### 7.4 RAG 预取延迟降低验证

`scripts/benchmark_prefetch.py` 验证 RAG 预取效果（#10）：

```python
async def benchmark_prefetch():
    """对比 RAG 预取开启/关闭的延迟差异"""
    metrics_a = await run_without_prefetch(250_queries)  # 基准
    metrics_b = await run_with_prefetch(250_queries)     # 预取
    
    from pprint import pprint
    pprint({
        "avg_latency_without_prefetch_ms": metrics_a.avg_latency_ms,
        "avg_latency_with_prefetch_ms": metrics_b.avg_latency_ms,
        "reduction_pct": round((1 - metrics_b.avg_latency_ms / metrics_a.avg_latency_ms) * 100, 1),
    })
```

## 8. 项目结构与新增文件

```
scripts/
├── generate_knowledge_base.py    # [新增] 知识库生成
├── import_real_docs.py           # [新增] 真实数据导入
├── evaluate_rag.py               # [新增] RAG 评测
├── benchmark_cache.py            # [新增] 缓存命中率测试
├── benchmark_latency.py          # [新增] 流式延迟测试
├── benchmark_ab_test.py          # [新增] A/B 对比测试
├── benchmark_cost.py             # [新增] 成本分析
├── benchmark_prefetch.py         # [新增] RAG 预取效果
└── benchmark_cache_hierarchy.py  # [新增] 三级缓存分层命中率

data/
└── knowledge_base/               # [新增] 知识库数据目录
    └── .gitkeep

tests/eval/
├── rag_benchmark.json            # [新增] 评测主文件
├── queries/                      # [新增] 查询子集
└── golden/                       # [新增] 标准答案

reports/                          # [新增] 报告输出目录
├── .gitkeep
├── *.json                        # 各 benchmark 输出
└── *.md                          # 生成的 README 续篇

api/middleware/
└── trace_middleware.py           # [新增] Trace ID 中间件

web/
├── widget.html                   # [修改] 语音+图片上传
└── src/admin-settings.js         # [修改] 语音设置

docs/reports/releases/
└── changelog.md                  # [修改] 更新日志
```

## 9. 测试计划

| 测试文件 | 类型 | 覆盖范围 |
|---------|------|---------|
| `tests/eval/rag_benchmark.json` | 评测集 | 召回率/准确率/MRR |
| `tests/e2e/test_scenarios.py` | E2E | 四大场景路由 + 对话流程 |
| `tests/e2e/test_multimodal.py` | E2E | 语音/图片多模态链路 |
| `tests/e2e/test_trace.py` | E2E | Trace ID 全链路传播 |
| `tests/integration/test_audio_pipeline.py` | 集成 | AudioProcessor + Whisper |
| `tests/integration/test_kb_generation.py` | 集成 | 知识库生成 + 导入 |
| `tests/unit/test_cache_metrics.py` | 单元 | 缓存命中率统计 |

## 10. 实施顺序与依赖关系

```
第1层 (并行)
├─ scripts/generate_knowledge_base.py    ← 无依赖
├─ media/ 链路完善 + 前端增强            ← 无依赖  
├─ trace_middleware.py (+ trace_id 传播)  ← 无依赖
├─ Prometheus 指标补全                    ← 无依赖
└─ container.py 组件注册补齐              ← 无依赖
    │
    ↓ 第1层集成验证
    │
第2层 (并行)
├─ 四大场景路由 + Agent 增强             ← 依赖知识库
├─ tests/eval/rag_benchmark.json          ← 依赖知识库
├─ scripts/benchmark_cache_hierarchy.py   ← 依赖缓存层
└─ scripts/benchmark_prefetch.py          ← 依赖知识库
    │
    ↓ 第2层集成验证
    │
第3层 (并行)
├─ scripts/evaluate_rag.py                ← 依赖评测集
├─ scripts/benchmark_cache.py             ← 依赖缓存
├─ scripts/benchmark_latency.py           ← 依赖流式链路
└─ scripts/benchmark_ab_test.py           ← 依赖全部
    │
    ↓ 第3层集成验证
    │
第4层 (收尾)
├─ docs/ 对齐（README, changelog）
├─ 覆盖率修复
└─ 最终验证
```

## 11. 验收标准总表

| # | 验收标准 | 验证方式 |
|---|---------|---------|
| 1 | 知识库文档数 ≥ 5,000 | `python -m scripts.generate_knowledge_base --count` |
| 2 | 多模态输入（图片/语音）端到端可用 | e2e test + 手动测试 |
| 3 | 四大场景路由准确率 ≥ 80% | `pytest tests/e2e/test_scenarios.py -v` |
| 4 | 评测集 ≥ 500 条 | `wc -l tests/eval/rag_benchmark.json` |
| 5 | Top-3 召回率 ≥ 91% | `make eval-rag` |
| 6 | 缓存命中率 ~65-70% | `make benchmark` |
| 7 | LLM 调用量降低 ≥ 40% | `scripts/benchmark_ab_test.py` |
| 8 | Token 成本下降 ≥ 35% | `scripts/benchmark_cost.py` |
| 9 | 流式首字 P99 < 2s | `scripts/benchmark_latency.py` |
| 10 | RAG 预取延迟降低 ≥ 20% | `scripts/benchmark_prefetch.py` |
| 11 | 15+ 核心组件注册 | `python -c "from core.container import Container; ..."` |
| 12 | 20+ Prometheus 指标 | `curl localhost:8000/metrics | grep '^#' | wc -l` |
| 13 | 5 种协作模式全测试覆盖 | `pytest tests/` 通过 |
| 14 | Trace ID 全链路传播 | e2e test 验证 |
| 15 | 三级缓存分层命中率统计 | `scripts/benchmark_cache_hierarchy.py` |
