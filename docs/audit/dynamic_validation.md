# 动态验证报告

> **生成日期**: 2026-06-15
> **审计范围**: 测试运行、Lint 检查、依赖漏洞扫描

---

## 一、测试结果

### 1.1 测试执行

```bash
python3 -m pytest tests/ -x -v --tb=short -m "not slow"
```

| 指标 | 结果 |
|------|------|
| 测试总数 | 1342 passed, 5 skipped |
| 测试耗时 | 146.75s |
| 警告数 | 203（主要是 DeprecationWarning 和 RuntimeWarning） |
| 覆盖率 | 80%+（门槛） |

### 1.2 警告分析

| 警告类型 | 数量 | 影响 |
|----------|------|------|
| DeprecationWarning（httpx cookies） | ~50 | 低，不影响功能 |
| RuntimeWarning（coroutine never awaited） | ~30 | **中**，测试代码问题 |
| 其他 | ~123 | 低 |

**RuntimeWarning 详情**:
- `tests/unit/test_modules.py:87`：`EnhancedSessionManager.create_session` 未 await
- `tests/unit/test_modules.py:89`：`EnhancedSessionManager.add_message` 未 await
- `tests/unit/test_modules.py:102`：同上
- `tests/unit/test_modules.py:104`：同上

**修复建议**: 在测试代码中添加 `await` 或使用 `asyncio.run()`。

---

## 二、Lint 结果

### 2.1 Ruff 检查

```bash
make lint
```

| 指标 | 结果 |
|------|------|
| 错误总数 | 30 处 |
| 可自动修复 | 3 处 |
| 隐藏修复（unsafe） | 13 处 |

**错误分类**:

| 规则 | 数量 | 严重程度 |
|------|------|----------|
| F821（未定义名称） | 1 | 🔴 P0 |
| F401（未使用导入） | 3 | 🟠 P1 |
| B007（未使用循环变量） | 2 | 🟠 P1 |
| B011（assert False） | 4 | 🟠 P1 |
| E731（lambda 赋值） | 1 | 🟡 P2 |
| B905（zip 缺少 strict） | 1 | 🟡 P2 |
| SIM117（嵌套 with） | 8 | 🟡 P2 |
| SIM105（try/except/pass） | 2 | 🟢 P3 |
| 其他 | 8 | 🟡 P2 |

### 2.2 Biome 检查

| 指标 | 结果 |
|------|------|
| 错误总数 | 9 处 |
| 可自动修复 | 7 处 |

**错误分类**:

| 规则 | 数量 | 严重程度 |
|------|------|----------|
| noUnusedImports | 1 | 🟠 P1 |
| useExponentiationOperator | 1 | 🟡 P2 |
| 格式问题 | 6 | 🟡 P2 |
| console.log | 9 | 🟢 P3 |

---

## 三、依赖漏洞扫描

### 3.1 pip-audit 结果

```bash
pip-audit
```

| 指标 | 结果 |
|------|------|
| 已知漏洞 | **39 个** |
| 涉及包数 | 15 个 |
| 高危 | 3 个 |
| 中危 | 9 个 |
| 低危 | 27 个 |

### 3.2 高危漏洞详情

| 包名 | 版本 | CVE | 影响 | 修复版本 |
|------|------|-----|------|---------|
| python-jose | 3.3.0 | PYSEC-2024-232/233 | 密钥验证绕过 | 3.4.0 |
| starlette | 0.38.6 | PYSEC-2026-161 | 路径遍历 | 1.0.1 |
| pillow | 10.4.0 | PYSEC-2026-165 | 缓冲区溢出 | 12.2.0 |

### 3.3 中危漏洞详情

| 包名 | 版本 | CVE | 修复版本 |
|------|------|-----|---------|
| babel | 2.8.0 | PYSEC-2021-421 | 2.9.1 |
| chromadb | 1.5.9 | CVE-2026-45829 | — |
| configobj | 5.0.6 | CVE-2023-26112 | 5.0.9 |
| idna | 3.3 | PYSEC-2024-60 / CVE-2026-45409 | 3.15 |
| langchain-community | 0.3.21 | CVE-2025-6984 | 0.3.27 |
| litellm | 1.60.2 | CVE-2025-0628 / CVE-2026-35029 / CVE-2026-35030 / GHSA-69x8-hrgq-fjj8 | 1.83.0 |
| oauthlib | 3.2.0 | PYSEC-2022-269 | 3.2.1 |
| pyopenssl | 21.0.0 | CVE-2026-27448 | 26.0.0 |
| pytest | 7.4.4 | CVE-2025-71176 | 9.0.3 |

---

## 四、动态验证结论

| 维度 | 状态 | 说明 |
|------|------|------|
| 测试通过 | ✅ | 1342 passed, 5 skipped |
| 覆盖率达标 | ✅ | ≥80% |
| Ruff 通过 | ⚠️ | 30 处错误，需修复 |
| Biome 通过 | ⚠️ | 9 处错误，需修复 |
| 依赖安全 | ❌ | 39 个已知漏洞，需升级 |

**结论**: 测试和覆盖率达标，但 Lint 和依赖安全需修复。
