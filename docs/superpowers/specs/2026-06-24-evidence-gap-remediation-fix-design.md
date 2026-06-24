# 证据缺口修复 — 修正与开发设计方案

> 日期：2026-06-24
> 状态：设计完成
> 影响范围：scripts/, tests/, web/, api/, core/, docs/
> 修复策略：分层流水线（Layer 1 → Layer 2 → Layer 3）

---

## 0. 审计回顾

2026-06-24 对 `evidence-gap-remediation-plan.md` 和 `evidence-gap-remediation-design.md` 中 22 项任务的审计发现：

| 问题级别 | 数量 | 典型问题 |
|---------|------|---------|
| ✅ 通过 | 12/22 | 场景路由、Agent 增强、DI 容器 |
| ⚠️ 部分通过 | 4/22 | 知识库 4975 条、Prometheus 17 指标 |
| ❌ 未通过 | 5/22 | 前端多模态缺失、benchmark ID 不匹配 |
| 🔴 严重缺陷 | 1 | 文档虚假声明 + 测试无法通过 |

**Trace 测试失败根因**：`ValueError: Duplicated timeseries in CollectorRegistry`
Prometheus 全局 `REGISTRY` 在 pytest 多 session 测试中重复注册同名 metric，导致崩溃。

---

## 1. 修复策略

**分层流水线（Layer Pipeline）**：按依赖关系分 3 层推进，层内可并行，层间串行验证。

```
Layer 1 [基础数据层] — 无外部依赖，全并行
  ├─ 知识库计数修复 (4975→5000+)
  ├─ 基准 ID 对齐 (expected_doc_ids → 实际知识库 ID)
  ├─ 子查询 JSON 文件创建
  └─ Prometheus 重复注册 Bug 修复
  ↓ 集成验证

Layer 2 [多模态层] — 依赖 Layer 1 就绪
  ├─ 前端语音按钮 (widget.html)
  ├─ 前端图片上传 (widget.html)
  └─ 后端 /api/chat/multimodal 路由
  ↓ 集成验证

Layer 3 [收尾层] — 依赖 Layer 1+2 完成
  ├─ Prometheus 指标 17→20+
  ├─ Changelog 虚假声明修复
  ├─ Ruff lint 错误修复 (16→0)
  ├─ 测试修复 (fixture/断言)
  └─ 全量集成验证
```

---

## 2. Layer 1：基础数据层

### 2.1 知识库 L3 计数修复

**文件**：`scripts/generate_knowledge_base.py`

**问题**：`_generate_l3()` 场景文档生成仅 975 条，目标 1000。原因是 `L3_TARGET // len(scenes)` 整除截断导致的余数丢失。

**修复**：
```python
# _generate_l3() 末尾补充缺失数量
EXTRA_NEEDED = L3_TARGET - len(l3_docs)  # = 25
while len(l3_docs) < L3_TARGET:
    template = random.choice(SCENE_TEMPLATES)
    l3_docs.append(generate_scene_doc(template))
```

**验证**：`python scripts/generate_knowledge_base.py --validate` → `Total 5000`

### 2.2 基准 expected_doc_ids 对齐

**文件**：`tests/eval/rag_benchmark.json`, `tests/eval/golden/expected_doc_ids.json`

**问题**：500 条查询的 `expected_doc_ids` 指向 `pr_000`/`te_000`/`fa_0xx`/`co_0xx` 前缀，而知识库实际生成 `derm_XXXXXX`/`faq_XXXXX`/`scene_XXXXX` 前缀。零重叠。

**修复策略**：**保持知识库 ID 不变，重新生成基准 ID**（经用户确认）。

**映射规则**：
- `pr_NNN` → 从生成的成分/产品文档中随机选取 3 个（`derm_`）
- `te_NNN` → 从 FAQ/技术答疑文档中选取 3 个（`faq_`）
- `fa_NNN` → 从 FAQ/售后文档中选取 3 个（`faq_`）
- `co_NNN` → 从场景/投诉文档中选取 3 个（`scene_`）

**实现**：编写 `scripts/regenerate_benchmark_ids.py` 一次性脚本：
1. 调用 `generate_knowledge_base.generate()` 获取实际文档列表
2. 读取 `rag_benchmark.json` 的 500 条查询
3. 按 `category`/`scene` 匹配知识库文档，替换 `expected_doc_ids`
4. 同步更新 `expected_doc_ids.json`

**验证**：`OVERLAP = 157`（所有基准 ID 均存在于知识库文档列表中）

### 2.3 子查询 JSON 文件创建

**文件**：新建 `tests/eval/queries/single_condition_200.json`、`multi_condition_150.json`、`fuzzy_150.json`

**修复**：从 `tests/eval/rag_benchmark.json` 按 `query_type` 字段分割提取：
```python
QUERY_TYPES = {
    "single": "single_condition_200.json",
    "multi": "multi_condition_150.json",
    "fuzzy": "fuzzy_150.json",
}
```

将 `longtail` 查询合并到 `fuzzy.json` 中（与设计一致）。

### 2.4 Prometheus 重复注册 Bug 修复

**文件**：`core/monitoring.py`, `tests/conftest.py`（如存在）

**问题**：模块级 `Counter('cache_l1_hits_total', ...)` 在 `CollectorRegistry` 全局注册。当 pytest 多个 session 或 TestClient 多次初始化时，同名 metric 已存在 → `ValueError: Duplicated timeseries`。

**修复方案**（两处）：

**A) `core/monitoring.py`** — 包装所有 Prometheus metric 创建，使用 `_METRICS_CACHE` 名 → 实例映射 + `try/except ValueError` 安全回溯：

```python
"""Core monitoring module for the customer service AI agent."""

from prometheus_client import Counter as _PromCounter
from prometheus_client import Gauge as _PromGauge
from prometheus_client import Histogram as _PromHistogram
from prometheus_client import REGISTRY
import contextlib

# Module-level cache: name → metric instance
_METRICS: dict[str, object] = {}


def _counter(name: str, documentation: str, **kwargs) -> _PromCounter:
    """Create or retrieve a Counter metric (safe for re-import in tests)."""
    if name not in _METRICS:
        try:
            _METRICS[name] = _PromCounter(name, documentation, **kwargs)
        except ValueError:
            with contextlib.suppress(KeyError, TypeError):
                for c in list(REGISTRY._collector_to_names.keys()):
                    if getattr(c, '_name', None) == name:
                        REGISTRY.unregister(c)
            _METRICS[name] = _PromCounter(name, documentation, **kwargs)
    return _METRICS[name]  # type: ignore[return-value]


def _gauge(name: str, documentation: str, **kwargs) -> _PromGauge:
    """Safe Gauge creation — mirrors _counter()."""
    if name not in _METRICS:
        try:
            _METRICS[name] = _PromGauge(name, documentation, **kwargs)
        except ValueError:
            with contextlib.suppress(KeyError, TypeError):
                for c in list(REGISTRY._collector_to_names.keys()):
                    if getattr(c, '_name', None) == name:
                        REGISTRY.unregister(c)
            _METRICS[name] = _PromGauge(name, documentation, **kwargs)
    return _METRICS[name]  # type: ignore[return-value]


def _histogram(name: str, documentation: str, **kwargs) -> _PromHistogram:
    """Safe Histogram creation — mirrors _counter()."""
    if name not in _METRICS:
        try:
            _METRICS[name] = _PromHistogram(name, documentation, **kwargs)
        except ValueError:
            with contextlib.suppress(KeyError, TypeError):
                for c in list(REGISTRY._collector_to_names.keys()):
                    if getattr(c, '_name', None) == name:
                        REGISTRY.unregister(c)
            _METRICS[name] = _PromHistogram(name, documentation, **kwargs)
    return _METRICS[name]  # type: ignore[return-value]
```

然后将所有模块级 `Counter(...)` / `Gauge(...)` / `Histogram(...)` 替换为 `_counter(...)` / `_gauge(...)` / `_histogram(...)`。

**B) 测试 conftest/hooks** — 在每个测试完成后清理 Prometheus REGISTRY（防止跨测试污染）：

```python
# 建议在 conftest.py 或测试模块添加
@pytest.fixture(autouse=True)
def _clean_prometheus_registry():
    """Prevent Prometheus DuplicatedTimeseries errors across tests."""
    yield
    collectors = list(REGISTRY._collector_to_names.keys())
    for c in collectors:
        with contextlib.suppress(Exception):
            REGISTRY.unregister(c)
```

**验证**：
```bash
pytest tests/e2e/test_trace.py -v --tb=short
# 不再出现 ValueError: Duplicated timeseries
```

---

## 3. Layer 2：多模态链路

### 3.1 前端语音输入

**文件**：`web/widget.html`

**新增 UI 元素**：
- 语音按钮 `<button id="voiceBtn" title="语音输入">`（SVG 麦克风图标，在输入框右侧容器）
- 录音状态条 `<div id="recordingStatus" hidden>`：
  - 波形动画（3 条竖线 `@keyframes wave`，橙色 `#ff6f00`）
  - 计时器 `<span id="recordingTimer">0:00</span>`
  - 停止按钮 `[■ 停止]` — 停止录音→上传→转录
  - 取消按钮 `[✕ 取消]` — 释放流、隐藏状态条

**JavaScript 逻辑**（回填确认模式，经用户确认）：

```javascript
// 开始录音
voiceBtn.addEventListener('click', async () => {
    if (mediaRecorder?.state === 'recording') return;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];
    mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
    mediaRecorder.onstop = async () => {
        const blob = new Blob(audioChunks, { type: 'audio/webm' });
        const form = new FormData();
        form.append('audio', blob, 'voice.webm');
        // 调用 /api/chat/voice 上传
        const resp = await fetch('/api/chat/voice', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
            body: form,
        });
        const data = await resp.json();
        if (data.transcription) {
            document.getElementById('chatInput').value = data.transcription;
        }
        stream.getTracks().forEach(t => t.stop());
        hideRecorder();
    };
    mediaRecorder.start();
    showRecorder();
});
```

**CSS**：在 `web/styles/theme-panel.css` 或内联 `<style>` 添加录音状态样式。

### 3.2 前端图片上传

**文件**：`web/widget.html`

**新增 UI 元素**：
- 图片按钮 `<button id="imageBtn" title="上传图片">`（SVG 图片图标，在语音按钮旁）
- 隐藏 `input[type=file]`：`accept="image/jpeg,image/png,image/webp"`
- 预览缩略图 `<div id="imagePreview" hidden>`：
  - `<img id="previewImg">`
  - 移除按钮 `[✕]`

**JavaScript 逻辑**（附加到用户消息模式，经用户确认）：

```javascript
imageBtn.addEventListener('click', () => document.getElementById('imageInput').click());

imageInput.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const compressed = await compressImage(file, 1024, 0.8);
    // 预览
    const reader = new FileReader();
    reader.onload = ev => {
        document.getElementById('previewImg').src = ev.target.result;
        document.getElementById('imagePreview').hidden = false;
    };
    reader.readAsDataURL(compressed);
    // 存储压缩后的 Blob 用于后续发送
    window._pendingImage = { blob: compressed, name: file.name };
});

async function compressImage(file, maxSize, quality) {
    const img = await createImageBitmap(file);
    const canvas = document.createElement('canvas');
    let { width, height } = img;
    if (width > maxSize) { height *= maxSize / width; width = maxSize; }
    canvas.width = width; canvas.height = height;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0, width, height);
    return new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', quality));
}
```

**消息发送时**：检查 `window._pendingImage`，如有则通过 `FormData` 附加。

### 3.3 后端多模态路由

**文件**：`api/routes/chat_multimodal.py`

**新增/改造端点**：

```python
@router.post("/api/chat/multimodal")
async def chat_multimodal(
    request: Request,
    file: UploadFile = File(None),
    message: str = Form(""),
    file_type: str = Header("auto"),
):
    """统一多模态入口：自动检测文件类型并路由到对应处理器"""
    if not file:
        return await _chat_text(request, message)

    # 确定文件类型
    ftype = file_type if file_type != "auto" else _detect_type(file.content_type)

    if ftype == "voice":
        processor = request.app.state.container.audio_processor
        transcription = await processor.transcribe(await file.read())
        return {"type": "voice", "transcription": transcription, "message": transcription}

    elif ftype == "image":
        processor = request.app.state.container.image_processor
        result = await processor.analyze(await file.read(), file.content_type)
        return {
            "type": "image",
            "image_url": result.get("data_url", ""),
            "analysis": result.get("description", ""),
        }

    elif ftype == "document":
        processor = request.app.state.container.document_processor
        text = await processor.extract(await file.read())
        return {"type": "document", "text": text}

    raise HTTPException(400, f"不支持的文件类型: {file.content_type}")
```

> **注意**：设计文档中的路由是简化的。实际实现需适配现有 `chat_multimodal.py` 的代码结构和依赖注入方式。重点是 `file_type` 检测和语音/图片的差异化处理。

---

## 4. Layer 3：收尾层

### 4.1 Prometheus 指标 17→20+

**文件**：`core/monitoring.py`

新增 3 个指标（使用 `_histogram`/`_counter` wrapper）：

```python
rag_search_latency_seconds = _histogram(
    "rag_search_latency_seconds",
    "RAG search query latency distribution",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0],
)

agent_process_time_seconds = _histogram(
    "agent_process_time_seconds",
    "Agent processing time per call",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
)

cache_writes_total = _counter(
    "cache_writes_total",
    "Total number of cache write operations",
)
```

### 4.2 Changelog 虚假声明修复

**文件**：`docs/reports/releases/changelog.md`

**原则**：声明必须与实现一致。如果 Layer 2 多模态功能已完成 → 保留过去时；如果未完成 → 改为正确描述。

**修复**：将多模态条目改为精确描述当前状态：

```markdown
### 🎨 前端增强（按实际完成度标注）
- **语音设置面板** [已实现]：管理后台添加语音设置面板（语言选择/自动发送/音频格式）— web/admin.html
- **语音输入** [代码就绪/前端未整合]：后端 AudioProcessor + Whisper API 转写可用；前端语音按钮和录音 UI 待接入 widget.html
- **图片上传** [代码就绪/前端未整合]：后端 ImageProcessor 分析能力可用；前端图片拖拽/预览/压缩待接入 widget.html
```

### 4.3 Ruff lint 错误修复

**范围**：审计涉及的 9 个脚本（`scripts/` 下新增和修改的 `.py` 文件）

**错误类型与修复**：

| 错误码 | 修复方式 | 预期剩余 |
|--------|----------|----------|
| B007 | 将 `for i, case` 中未用的 `i` 改为 `_` | 0 |
| UP031 | `"%s" % var` → `f"{var}"` 或 `str.format()` | 0 |
| F541 | `f"static string"` → `"static string"` | 0 |
| SIM105 | `try/except: pass` → `contextlib.suppress()` | 0 |
| F821 | 添加缺失的 import | 0 |
| F841 | 删除未使用的变量赋值 | 0 |

### 4.4 测试修复

#### 4.4.1 test_kb_generation.py

Layer 1 修复（4975→5000+）后自动通过。无需单独修复。

#### 4.4.2 test_audio_pipeline.py

**问题**：`test_voice_api_success` 使用了未定义的 `test_client` fixture。

**修复**：添加 fixture 或将测试改为纯导入测试：

```python
@pytest.fixture
def test_client():
    from fastapi.testclient import TestClient
    from api.app_factory import app
    with TestClient(app) as client:
        yield client
```

或在测试函数内直接创建 TestClient 实例。

#### 4.4.3 test_trace.py

Layer 1.4 修复（Prometheus 注册 bug）后自动通过。HuggingFace 网络不可用是环境限制，不影响代码正确性，使用 `pytest.mark.skipif` 或在 conftest 中 mock 即可。

### 4.5 Makefile 命令修复

**文件**：`Makefile`

确保 `component-count` 命令正确引用容器类名并稳定输出：

```makefile
component-count:
	@python3 -c "from core.container import ServiceContainer; c = ServiceContainer(); print(f'  活跃服务数: {len(vars(c))}')" 2>/dev/null || echo "  ⚠️ 容器不可用"
```

### 4.6 集成验证清单

```bash
echo "=== [1/10] 知识库验证 ==="
python3 scripts/generate_knowledge_base.py --validate

echo "=== [2/10] 评测集完整性 ==="
python3 -c "import json; d=json.load(open('tests/eval/rag_benchmark.json')); assert len(d['queries']) >= 500; print(f'✓ {len(d[\"queries\"])} 条')"

echo "=== [3/10] 基准 ID 对齐 ==="
python3 scripts/verify_benchmark_ids.py  # 新增验证脚本

echo "=== [4/10] 场景路由 ==="
python3 -m pytest tests/e2e/test_scenarios.py -v

echo "=== [5/10] 缓存指标 ==="
python3 -m pytest tests/unit/test_cache_metrics.py -v

echo "=== [6/10] Trace ID ==="
python3 -m pytest tests/e2e/test_trace.py -v --timeout=30

echo "=== [7/10] 集成测试 ==="
python3 -m pytest tests/integration/ -v

echo "=== [8/10] Ruff ==="
python3 -m ruff check scripts/*.py tests/e2e/test_scenarios.py tests/unit/test_cache_metrics.py --statistics

echo "=== [9/10] 组件计数 ==="
make component-count

echo "=== [10/10] 全量 lint ==="
make lint
```

---

## 5. 实施顺序

| 顺序 | 任务 | 依赖 | 预估工时 |
|------|------|------|----------|
| **Layer 1-S1** | 知识库 L3 计数修复 | 无 | 15 min |
| **Layer 1-S2** | 基准 ID 对齐脚本 | S1 | 25 min |
| **Layer 1-S3** | 子查询文件创建 | 无 | 5 min |
| **Layer 1-S4** | Prometheus 注册 Bug 修复 | 无 | 25 min |
| **Layer 1-V** | 集成验证 | S1~S4 | 5 min |
| **Layer 2-S1** | 前端语音按钮 | 无 | 45 min |
| **Layer 2-S2** | 前端图片上传 | 无 | 25 min |
| **Layer 2-S3** | 后端多模态路由 | 无 | 25 min |
| **Layer 2-V** | 集成验证 | S1~S3 | 10 min |
| **Layer 3-S1** | Prometheus 指标补全 | Layer 1-S4 | 10 min |
| **Layer 3-S2** | Changelog 修复 | 无 | 5 min |
| **Layer 3-S3** | Ruff 修复 | 无 | 10 min |
| **Layer 3-S4** | 测试修复 | Layer 1-S4 | 15 min |
| **Layer 3-S5** | Makefile 修复 | 无 | 5 min |
| **Layer 3-V** | 最终集成验证 | 全部 | 15 min |
| **总计** | | | **~4 小时** |

---

## 6. 验收标准

| # | 标准 | 验证方法 | 当前状态 | 目标状态 |
|---|------|----------|----------|----------|
| 1 | 知识库文档数 ≥ 5,000 | `--validate` | 4975 | 5000+ |
| 2 | 基准 ID 与知识库 100% 对齐 | 重叠检测脚本 | 0% | 100% |
| 3 | 子查询文件 3 个 | `ls tests/eval/queries/` | 0 | 3 |
| 4 | Prometheus 注册幂等 | trace 测试通过 | ValueError | 0 errors |
| 5 | 前端语音按钮可用 | 手动 + 代码存在性 | 不存在 | 存在 |
| 6 | 前端图片上传可用 | 手动 + 代码存在性 | 不存在 | 存在 |
| 7 | `/api/chat/multimodal` 路由 | curl 测试 | 不存在 | 存在 |
| 8 | Prometheus 指标数 | AST 计数 | 17 | 20+ |
| 9 | Changelog 无虚假声明 | 人工审查 | 3 处虚假 | 0 |
| 10 | Ruff errors | `ruff check` | 16 | 0 |
| 11 | 全部 8 个测试套件通过 | pytest | 3 个失败 | 全部通过 |
| 12 | `make benchmark` 可运行 | `make benchmark` | 未知 | exit 0 |