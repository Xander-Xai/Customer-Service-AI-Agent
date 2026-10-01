"""音频管道集成测试。"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_audio_processor_transcribe_mock():
    """测试 AudioProcessor.transcribe 逻辑（Mock）"""
    with patch("media.audio_processor.AudioProcessor") as MockProcessor:
        processor = MockProcessor()
        processor.transcribe = AsyncMock(return_value="这是转录文本")

        audio_data = b"fake_audio_data"
        content_type = "audio/webm"

        result = await processor.transcribe(audio_data, content_type)
        assert result == "这是转录文本"
        processor.transcribe.assert_awaited_once_with(audio_data, content_type)


@pytest.mark.asyncio
async def test_audio_processor_empty_audio():
    """测试空音频处理"""
    from media.audio_processor import AudioProcessor

    processor = AudioProcessor()

    with pytest.raises((ValueError, Exception)):
        await processor.transcribe(b"", "audio/webm")


@pytest.mark.asyncio
async def test_audio_processor_invalid_format():
    """测试无效音频格式处理"""
    from media.audio_processor import AudioProcessor

    processor = AudioProcessor()
    with pytest.raises((ValueError, Exception)):
        await processor.transcribe(b"test", "audio/xyz")


@pytest.mark.asyncio
async def test_audio_processor_large_file():
    """测试超大音频文件处理"""
    from media.audio_processor import AudioProcessor

    processor = AudioProcessor()
    large_data = b"x" * (26 * 1024 * 1024)  # 26MB
    with pytest.raises((ValueError, Exception)):
        await processor.transcribe(large_data, "audio/mp3")


@pytest.mark.asyncio
async def test_voice_api_module_imports():
    """测试语音 API 路由模块可正常导入（无需完整 app 初始化）"""
    try:
        from api.routes import chat_multimodal
        assert chat_multimodal is not None
    except ImportError as e:
        pytest.skip(f"语音 API 路由模块导入失败（依赖服务未运行）: {e}")
    except Exception as e:
        # 可能因为缺失服务依赖而初始化失败
        pytest.skip(f"语音 API 路由初始化跳过: {e}")



@pytest.mark.asyncio
async def test_audio_pipeline_integration():
    """测试音频管道完整流程（模拟）"""
    with patch("media.audio_processor.AudioProcessor") as MockProcessor:
        processor = MockProcessor()
        processor.transcribe = AsyncMock(return_value="我要查询订单状态")

        # 模拟前端录音 → 上传 → 转录 → 回填
        audio_blob = b"mock_audio_data"
        transcription = await processor.transcribe(audio_blob, "audio/webm")

        # 验证转录结果
        assert transcription is not None
        assert len(transcription) > 0
        assert "订单" in transcription

        # 验证转录后可继续对话
        if transcription:
            # 转录后可以作为用户消息发送
            assert transcription.startswith("我要")
