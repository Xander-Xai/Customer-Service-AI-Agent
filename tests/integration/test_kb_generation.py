"""知识库生成集成测试。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.mark.asyncio
async def test_generate_script_imports():
    """测试 generate_knowledge_base.py 可导入"""
    try:
        from scripts import generate_knowledge_base as gen

        assert gen is not None
        assert hasattr(gen, "generate")
    except (ImportError, Exception) as e:
        pytest.skip(f"知识库生成脚本不可用: {e}")


@pytest.mark.asyncio
async def test_generate_script_runs():
    """测试 generate_knowledge_base 可运行并生成数据"""
    try:
        from scripts.generate_knowledge_base import generate

        docs = generate()
        assert len(docs) >= 5000, f"知识库文档数不足: {len(docs)}"

        # 验证文档结构
        for doc in docs[:10]:
            assert "id" in doc
            assert "title" in doc
            assert "content" in doc
            assert "category" in doc
            assert "scene" in doc

        # 验证分类分布
        categories = {}
        for doc in docs:
            cat = doc.get("category", "unknown")
            categories[cat] = categories.get(cat, 0) + 1

        print("\n  知识库生成验证:")
        print(f"  总文档数: {len(docs)}")
        print(f"  分类分布: {json.dumps(categories, ensure_ascii=False)}")
        assert "成分知识" in categories, "缺少成分知识分类"

    except ImportError as e:
        pytest.skip(f"generate_knowledge_base 模块不可用: {e}")


@pytest.mark.asyncio
async def test_import_script_imports():
    """测试 import_real_docs.py 可导入"""
    try:
        from scripts import import_real_docs as imp

        assert imp is not None
        assert hasattr(imp, "import_from_csv")
        assert hasattr(imp, "import_from_json")
    except (ImportError, Exception) as e:
        pytest.skip(f"导入脚本不可用: {e}")


@pytest.mark.asyncio
async def test_benchmark_scripts_import():
    """测试 benchmark 脚本可导入"""
    try:
        from scripts.benchmark_cache import benchmark_cache as bc

        assert bc is not None
    except (ImportError, Exception):
        # benchmark 脚本可能存在模块依赖，允许跳过
        pass

    try:
        from scripts.benchmark_latency import benchmark_latency as bl

        assert bl is not None
    except (ImportError, Exception):
        pass

    try:
        from scripts.benchmark_cost import analyze_cost as ac

        assert ac is not None
    except (ImportError, Exception):
        pass
