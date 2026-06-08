"""
多模态功能测试 (v5.1)
覆盖：ImageProcessor / AgentState 多模态字段 / BaseAgent 多模态消息构造 / Vision LLM 选择
运行: pytest tests/test_multimodal.py -v
"""

import base64
import io
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. ImageProcessor 模块
# ═══════════════════════════════════════════════════════════════════════════════


class TestImageProcessor:
    """ImageProcessor 完整验证"""

    def _make_test_image(self, width=100, height=100, fmt="JPEG"):
        """生成测试图片字节"""
        from PIL import Image

        img = Image.new("RGB", (width, height), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        return buf.getvalue()

    def test_validate_mime_allowed(self):
        from media.image_processor import ImageProcessor

        p = ImageProcessor()
        assert p.validate_mime("image/jpeg") is True
        assert p.validate_mime("image/png") is True
        assert p.validate_mime("image/webp") is True

    def test_validate_mime_rejected(self):
        from media.image_processor import ImageProcessor

        p = ImageProcessor()
        assert p.validate_mime("image/gif") is False
        assert p.validate_mime("image/svg+xml") is False
        assert p.validate_mime("application/pdf") is False
        assert p.validate_mime("") is False

    def test_validate_size_within_limit(self):
        from media.image_processor import ImageProcessor

        p = ImageProcessor(max_size_mb=5)
        assert p.validate_size(b"x" * 1024) is True

    def test_validate_size_exceeds_limit(self):
        from media.image_processor import ImageProcessor

        p = ImageProcessor(max_size_mb=1)
        assert p.validate_size(b"x" * (2 * 1024 * 1024)) is False

    def test_process_returns_data_url(self):
        from media.image_processor import ImageProcessor

        p = ImageProcessor()
        data = self._make_test_image(200, 200)
        result = p.process(data, "image/jpeg")
        assert result.startswith("data:image/")
        assert ";base64," in result

    def test_process_rejects_bad_mime(self):
        from media.image_processor import ImageProcessor

        p = ImageProcessor()
        data = self._make_test_image()
        with pytest.raises(ValueError, match="不支持的图片格式"):
            p.process(data, "image/bmp")

    def test_process_rejects_oversized(self):
        # 使用随机噪声图片（压缩率低）确保超过 1MB
        import random

        from media.image_processor import ImageProcessor

        random_data = bytes(random.getrandbits(8) for _ in range(2 * 1024 * 1024))  # 2MB
        p = ImageProcessor(max_size_mb=1)
        # 直接用大文件测试 validate_size
        assert p.validate_size(random_data) is False
        # 测试 process 会抛出 ValueError
        with pytest.raises(ValueError, match="图片大小超出限制"):
            p.process(random_data, "image/jpeg")

    def test_process_compresses_large_image(self):
        """大图应被压缩（长边 <= 2048）"""
        from PIL import Image

        from media.image_processor import ImageProcessor

        p = ImageProcessor(max_long_edge=512)
        # 创建 1000x1000 图片
        data = self._make_test_image(1000, 1000)
        result = p.process(data, "image/jpeg")
        # 解码验证
        b64_part = result.split(";base64,")[1]
        decoded = base64.b64decode(b64_part)
        img = Image.open(io.BytesIO(decoded))
        assert max(img.size) <= 512

    def test_process_rgba_to_rgb(self):
        """RGBA 图片应转为 RGB（JPEG 不支持 alpha）"""
        from PIL import Image

        from media.image_processor import ImageProcessor

        p = ImageProcessor()
        img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        result = p.process(data, "image/png")
        # 应成功转换为 JPEG
        assert "data:image/jpeg" in result

    def test_get_info_returns_metadata(self):
        from media.image_processor import ImageProcessor

        data = self._make_test_image(300, 200)
        info = ImageProcessor.get_info(data, "image/jpeg")
        assert info["width"] == 300
        assert info["height"] == 200
        assert info["size_bytes"] > 0

    def test_process_png(self):
        """PNG 图片处理"""
        from media.image_processor import ImageProcessor

        p = ImageProcessor()
        data = self._make_test_image(100, 100, fmt="PNG")
        result = p.process(data, "image/png")
        assert result.startswith("data:image/")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. AgentState 多模态字段
# ═══════════════════════════════════════════════════════════════════════════════


class TestAgentStateMultimodal:
    """AgentState 多模态字段验证"""

    def test_state_has_multimodal_fields(self):
        from core.state import AgentState

        # 检查 TypedDict 包含新字段
        annotations = AgentState.__annotations__
        assert "multimodal_content" in annotations
        assert "has_multimodal" in annotations

    def test_state_multimodal_content_is_list(self):
        from core.state import AgentState

        # total=False，所以可以只设置部分字段
        state: AgentState = {
            "multimodal_content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
            ],
            "has_multimodal": True,
        }
        assert state["has_multimodal"] is True
        assert len(state["multimodal_content"]) == 1

    def test_state_backward_compatible(self):
        """不含多模态字段时应兼容旧代码"""
        from core.state import AgentState

        state: AgentState = {
            "session_id": "test",
            "customer_query": "hello",
        }
        # 不应报错
        assert state.get("has_multimodal") is None
        assert state.get("multimodal_content") is None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. BaseAgent 多模态消息构造
# ═══════════════════════════════════════════════════════════════════════════════


class TestBaseAgentMultimodal:
    """BaseAgent 多模态消息构造验证"""

    @pytest.mark.asyncio
    async def test_prepare_messages_text_only(self):
        """纯文本消息应使用字符串 content"""
        from agents.general_agent import GeneralAgent
        from session_manager import EnhancedSessionManager

        agent = GeneralAgent()
        sm = EnhancedSessionManager()
        agent.set_session_manager(sm)
        agent.set_llm(MagicMock())

        state = {
            "session_id": "test_mm_1",
            "customer_query": "你好",
        }
        session_id, messages, drift = await agent._prepare_llm_messages(state, "你是一个客服助手")
        # 最后一条消息应是 HumanMessage with string content
        human_msg = messages[-1]
        assert hasattr(human_msg, "content")
        assert isinstance(human_msg.content, str)
        assert "你好" in human_msg.content

    @pytest.mark.asyncio
    async def test_prepare_messages_with_multimodal(self):
        """含多模态内容时应构造 list content"""
        from agents.general_agent import GeneralAgent
        from session_manager import EnhancedSessionManager

        agent = GeneralAgent()
        sm = EnhancedSessionManager()
        agent.set_session_manager(sm)
        agent.set_llm(MagicMock())

        state = {
            "session_id": "test_mm_2",
            "customer_query": "这是什么产品？",
            "multimodal_content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc123"}},
            ],
            "has_multimodal": True,
        }
        session_id, messages, drift = await agent._prepare_llm_messages(state, "你是一个产品专家")
        # 最后一条消息应是 HumanMessage with list content
        human_msg = messages[-1]
        assert isinstance(human_msg.content, list)
        # 应包含文本部分和图片部分
        text_parts = [p for p in human_msg.content if p.get("type") == "text"]
        image_parts = [p for p in human_msg.content if p.get("type") == "image_url"]
        assert len(text_parts) == 1
        assert len(image_parts) == 1
        assert "这是什么产品？" in text_parts[0]["text"]


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Vision LLM 选择
# ═══════════════════════════════════════════════════════════════════════════════


class TestVisionLLMSelection:
    """Vision LLM 选择逻辑验证"""

    def test_get_effective_llm_text_only(self):
        """纯文本应使用默认 LLM"""
        from agents.general_agent import GeneralAgent

        agent = GeneralAgent()
        mock_llm = MagicMock()
        mock_vision = MagicMock()
        agent.set_llm(mock_llm)
        agent.set_vision_llm(mock_vision)

        state = {"customer_query": "你好"}
        result = agent._get_effective_llm(state)
        assert result is mock_llm

    def test_get_effective_llm_multimodal(self):
        """多模态应使用 Vision LLM"""
        from agents.general_agent import GeneralAgent

        agent = GeneralAgent()
        mock_llm = MagicMock()
        mock_vision = MagicMock()
        agent.set_llm(mock_llm)
        agent.set_vision_llm(mock_vision)

        state = {"has_multimodal": True, "customer_query": "分析图片"}
        result = agent._get_effective_llm(state)
        assert result is mock_vision

    def test_get_effective_llm_no_vision_fallback(self):
        """无 Vision LLM 时应回退到默认 LLM"""
        from agents.general_agent import GeneralAgent

        agent = GeneralAgent()
        mock_llm = MagicMock()
        agent.set_llm(mock_llm)
        # 不设置 vision_llm

        state = {"has_multimodal": True, "customer_query": "分析图片"}
        result = agent._get_effective_llm(state)
        assert result is mock_llm


# ═══════════════════════════════════════════════════════════════════════════════
# 5. LLM 客户端多模态格式
# ═══════════════════════════════════════════════════════════════════════════════


class TestLLMClientMultimodal:
    """LLM 客户端多模态消息格式验证"""

    def test_format_messages_list_content(self):
        """list content 应直接传递（多模态格式）"""
        from langchain_core.messages import HumanMessage

        from llm.client import OpenAICompatibleClient

        client = OpenAICompatibleClient(api_key="test", base_url="http://test", model="test")
        multimodal_content = [
            {"type": "text", "text": "这是什么？"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
        ]
        messages = [HumanMessage(content=multimodal_content)]
        formatted = client._format_messages(messages)

        assert len(formatted) == 1
        assert formatted[0]["role"] == "user"
        assert isinstance(formatted[0]["content"], list)
        assert len(formatted[0]["content"]) == 2

    def test_format_messages_string_content(self):
        """字符串 content 应正常处理"""
        from langchain_core.messages import HumanMessage

        from llm.client import OpenAICompatibleClient

        client = OpenAICompatibleClient(api_key="test", base_url="http://test", model="test")
        messages = [HumanMessage(content="你好")]
        formatted = client._format_messages(messages)

        assert formatted[0]["content"] == "你好"
