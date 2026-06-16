"""
Prompt 版本管理器测试 (v5.1)
覆盖：PromptManager 核心逻辑 + API 路由
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from core.prompt_manager import PromptManager


class TestPromptManager:
    """PromptManager 核心逻辑测试"""

    def test_get_prompt_returns_default_when_no_db(self):
        """无 DB 记录时返回默认 Prompt"""
        pm = PromptManager()
        pm._db_available = False
        result = pm.get_prompt("product_agent", default_prompt="默认产品专家提示词")
        assert result == "默认产品专家提示词"

    def test_get_prompt_caches_result(self):
        """DB 结果被缓存，TTL 内不重复查询"""
        pm = PromptManager(cache_ttl=60.0)

        # 模拟 DB 返回
        with patch.object(
            pm, "_load_from_db", return_value=("DB 版本 Prompt", "v1.0")
        ) as mock_load:
            result1 = pm.get_prompt("product_agent", default_prompt="默认")
            result2 = pm.get_prompt("product_agent", default_prompt="默认")

        assert result1 == "DB 版本 Prompt"
        assert result2 == "DB 版本 Prompt"
        # DB 只查了一次（第二次走缓存）
        mock_load.assert_called_once()

    def test_get_prompt_cache_expires(self):
        """缓存过期后重新查询 DB"""
        pm = PromptManager(cache_ttl=0.1)  # 100ms TTL

        with patch.object(
            pm,
            "_load_from_db",
            side_effect=[
                ("v1 Prompt", "v1.0"),
                ("v2 Prompt", "v2.0"),
            ],
        ) as mock_load:
            result1 = pm.get_prompt("tech_agent", default_prompt="默认")
            time.sleep(0.15)  # 等待缓存过期
            result2 = pm.get_prompt("tech_agent", default_prompt="默认")

        assert result1 == "v1 Prompt"
        assert result2 == "v2 Prompt"
        assert mock_load.call_count == 2

    def test_get_prompt_db_error_falls_back(self):
        """DB 异常时回退到默认 Prompt"""
        pm = PromptManager()

        with patch.object(pm, "_load_from_db", side_effect=Exception("DB 连接失败")):
            result = pm.get_prompt("billing_agent", default_prompt="默认计费提示词")

        assert result == "默认计费提示词"
        # DB 标记为不可用
        assert pm._db_available is False

    def test_get_prompt_db_returns_none_uses_default(self):
        """DB 返回 None 时使用默认 Prompt"""
        pm = PromptManager()

        with patch.object(pm, "_load_from_db", return_value=(None, "")):
            result = pm.get_prompt("complaint_agent", default_prompt="默认投诉提示词")

        assert result == "默认投诉提示词"

    def test_invalidate_clears_cache(self):
        """invalidate 清除缓存"""
        pm = PromptManager()
        pm._cache["test_agent"] = {"prompt": "cached", "version": "v1", "loaded_at": time.time()}

        pm.invalidate("test_agent")
        assert "test_agent" not in pm._cache

    def test_invalidate_all(self):
        """invalidate(None) 清除所有缓存"""
        pm = PromptManager()
        pm._cache["a"] = {"prompt": "a", "version": "v1", "loaded_at": time.time()}
        pm._cache["b"] = {"prompt": "b", "version": "v1", "loaded_at": time.time()}

        pm.invalidate()
        assert len(pm._cache) == 0

    def test_get_version_info_from_cache(self):
        """get_version_info 返回缓存中的版本信息"""
        pm = PromptManager()
        pm._cache["product_agent"] = {
            "prompt": "test",
            "version": "v2.0",
            "loaded_at": time.time(),
        }

        info = pm.get_version_info("product_agent")
        assert info["version"] == "v2.0"
        assert info["source"] == "database"

    def test_get_version_info_fallback(self):
        """get_version_info 无缓存时返回 fallback"""
        pm = PromptManager()

        info = pm.get_version_info("unknown_agent")
        assert info["version"] == "default"
        assert info["source"] == "fallback"

    def test_save_prompt(self):
        """save_prompt 写入 DB 并清除缓存"""
        pm = PromptManager()
        mock_db = MagicMock()

        with patch("db.database.get_db_session", return_value=mock_db):
            result = pm.save_prompt(
                agent_name="product_agent",
                prompt_text="新的产品专家 Prompt 内容...",
                version="v2.0",
                activate=True,
            )

        assert result["agent_name"] == "product_agent"
        assert result["version"] == "v2.0"
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()

    def test_save_prompt_activate_deactivates_others(self):
        """save_prompt(activate=True) 停用同 agent 的其他版本"""
        pm = PromptManager()
        mock_db = MagicMock()

        with patch("db.database.get_db_session", return_value=mock_db):
            pm.save_prompt(
                agent_name="tech_agent",
                prompt_text="新版技术支持 Prompt...",
                version="v3.0",
                activate=True,
            )

        # 验证调用了 update 来停用其他版本
        mock_db.query.return_value.filter.return_value.update.assert_called_once_with(
            {"is_active": 0}
        )

    def test_list_versions(self):
        """list_versions 返回版本列表"""
        pm = PromptManager()
        mock_pv = MagicMock()
        mock_pv.id = 1
        mock_pv.version = "v1.0"
        mock_pv.is_active = 1
        mock_pv.score_avg = 0.8
        mock_pv.feedback_count = 10
        mock_pv.prompt_text = "这是一个测试 Prompt"
        mock_pv.created_at = MagicMock()

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            mock_pv
        ]

        with patch("db.database.get_db_session", return_value=mock_db):
            versions = pm.list_versions("product_agent")

        assert len(versions) == 1
        assert versions[0]["version"] == "v1.0"
        assert versions[0]["is_active"] is True
        assert versions[0]["score_avg"] == 0.8

    def test_list_agents(self):
        """list_agents 返回 agent 列表"""
        pm = PromptManager()
        mock_db = MagicMock()
        mock_db.query.return_value.distinct.return_value.all.return_value = [
            ("product_agent",),
            ("tech_agent",),
        ]

        with patch("db.database.get_db_session", return_value=mock_db):
            agents = pm.list_agents()

        assert "product_agent" in agents
        assert "tech_agent" in agents

    def test_record_feedback_updates_score(self):
        """record_feedback 增量更新 score_avg"""
        pm = PromptManager()
        pm._cache["product_agent"] = {
            "prompt": "test",
            "version": "v1.0",
            "loaded_at": time.time(),
        }

        mock_pv = MagicMock()
        mock_pv.score_avg = 0.5
        mock_pv.feedback_count = 10
        mock_pv.version = "v1.0"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_pv

        with patch("db.database.get_db_session", return_value=mock_db):
            pm.record_feedback("product_agent", score=1.0)

        # (0.5 * 10 + 1.0) / 11 = 6.0 / 11 ≈ 0.5454
        assert mock_pv.score_avg == pytest.approx(6.0 / 11, abs=0.001)
        assert mock_pv.feedback_count == 11

    def test_record_feedback_no_cache_noop(self):
        """record_feedback 无缓存时不操作"""
        pm = PromptManager()
        # 不设置缓存，不应报错
        pm.record_feedback("unknown_agent", score=1.0)


class TestPromptManagerAPI:
    """Prompt 管理 API 路由测试"""

    def _build_app(self):
        from fastapi import FastAPI

        from api.routes.prompts import router as prompts_router

        app = FastAPI()
        app.include_router(prompts_router)
        pm = PromptManager()
        pm._db_available = False  # 禁用 DB 避免测试依赖
        app.state.prompt_manager = pm
        return app, pm

    def test_list_agents_empty(self):
        """GET /api/admin/prompts/agents -- 空列表"""
        from fastapi.testclient import TestClient

        app, pm = self._build_app()
        with (
            patch("core.prompt_manager.PromptManager.list_agents", return_value=[]),
            TestClient(app) as client,
        ):
            resp = client.get("/api/admin/prompts/agents")
        assert resp.status_code == 200
        assert resp.json()["agents"] == []

    def test_list_agents_with_data(self):
        """GET /api/admin/prompts/agents -- 有数据"""
        from fastapi.testclient import TestClient

        app, pm = self._build_app()
        with (
            patch(
                "core.prompt_manager.PromptManager.list_agents",
                return_value=["product_agent", "tech_agent"],
            ),
            TestClient(app) as client,
        ):
            resp = client.get("/api/admin/prompts/agents")
        assert resp.status_code == 200
        assert len(resp.json()["agents"]) == 2

    def test_list_versions(self):
        """GET /api/admin/prompts/{agent_name} -- 列出版本"""
        from fastapi.testclient import TestClient

        app, pm = self._build_app()
        mock_versions = [
            {
                "id": 1,
                "version": "v1.0",
                "is_active": True,
                "score_avg": 0.8,
                "feedback_count": 5,
                "prompt_preview": "test...",
                "created_at": "2026-01-01",
            },
        ]
        with (
            patch("core.prompt_manager.PromptManager.list_versions", return_value=mock_versions),
            TestClient(app) as client,
        ):
            resp = client.get("/api/admin/prompts/product_agent")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_create_version(self):
        """POST /api/admin/prompts/{agent_name} -- 创建新版本"""
        from fastapi.testclient import TestClient

        app, pm = self._build_app()
        mock_result = {
            "id": 1,
            "agent_name": "product_agent",
            "version": "v2.0",
            "is_active": 1,
            "created_at": "2026-01-01",
        }
        with (
            patch("core.prompt_manager.PromptManager.save_prompt", return_value=mock_result),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/api/admin/prompts/product_agent",
                json={
                    "prompt_text": "这是一个新的产品专家 Prompt，长度超过10字符",
                    "version": "v2.0",
                    "activate": True,
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["prompt"]["version"] == "v2.0"

    def test_create_version_validation(self):
        """POST /api/admin/prompts/{agent_name} -- Prompt 太短被拒绝"""
        from fastapi.testclient import TestClient

        app, pm = self._build_app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/admin/prompts/product_agent",
                json={"prompt_text": "短", "version": "v1.0"},
            )
        assert resp.status_code == 422  # Pydantic validation error

    def test_get_active_version(self):
        """GET /api/admin/prompts/{agent_name}/active -- 获取活跃版本"""
        from fastapi.testclient import TestClient

        app, pm = self._build_app()
        pm._cache["product_agent"] = {
            "prompt": "产品专家 Prompt 内容",
            "version": "v1.0",
            "loaded_at": time.time(),
        }
        with TestClient(app) as client:
            resp = client.get("/api/admin/prompts/product_agent/active")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "v1.0"
        assert data["source"] == "database"
        assert "prompt_text" in data

    def test_no_prompt_manager_returns_503(self):
        """无 PromptManager 时返回 503"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.routes.prompts import router as prompts_router

        app = FastAPI()
        app.include_router(prompts_router)
        app.state.prompt_manager = None

        with TestClient(app) as client:
            resp = client.get("/api/admin/prompts/agents")
        assert resp.status_code == 503


class TestBaseAgentPromptManagerIntegration:
    """BaseAgent + PromptManager 集成测试"""

    def test_format_system_prompt_uses_prompt_manager(self):
        """_format_system_prompt 优先使用 PromptManager 的 Prompt"""
        from agents.product_agent import ProductAgent

        agent = ProductAgent()
        mock_pm = MagicMock()
        mock_pm.get_prompt.return_value = "来自 DB 的 {self_name} 专用 Prompt"
        agent.set_prompt_manager(mock_pm)

        result = agent._format_system_prompt("默认的 {self_name} Prompt")
        assert "来自 DB 的" in result
        assert "产品专家" in result  # self_name 被替换

    def test_format_system_prompt_falls_back_to_template(self):
        """PromptManager 无记录时回退到默认模板"""
        from agents.product_agent import ProductAgent

        agent = ProductAgent()
        mock_pm = MagicMock()
        mock_pm.get_prompt.return_value = ""  # 空字符串 = 无 DB 版本
        agent.set_prompt_manager(mock_pm)

        result = agent._format_system_prompt("默认的 {self_name} Prompt")
        assert "默认的" in result
        assert "产品专家" in result

    def test_format_system_prompt_no_prompt_manager(self):
        """无 PromptManager 时使用默认模板"""
        from agents.product_agent import ProductAgent

        agent = ProductAgent()
        # 不设置 prompt_manager
        result = agent._format_system_prompt("默认的 {self_name} Prompt")
        assert "默认的" in result

    def test_format_system_prompt_manager_error(self):
        """PromptManager 异常时回退到默认模板"""
        from agents.product_agent import ProductAgent

        agent = ProductAgent()
        mock_pm = MagicMock()
        mock_pm.get_prompt.side_effect = Exception("DB 连接失败")
        agent.set_prompt_manager(mock_pm)

        result = agent._format_system_prompt("默认的 {self_name} Prompt")
        assert "默认的" in result
