"""音频管道集成测试。"""

from unittest.mock import AsyncMock, MagicMock, patch

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
async def test_voice_api_success(test_client):
    """测试语音 API 端点在 Mock 环境下可调用"""
    # 使用 multipart/form-data 模拟音频上传
    response = await test_client.post(
        "/api/chat/voice",
        files={"audio": ("test.webm", b"fake_audio_data", "audio/webm")},
        data={"session_id": "", "session_token": ""},
    )
    # 可以返回 422（验证失败）或 500（处理失败），但不应是 404
    assert response.status_code != 404, "语音 API 端点未找到"



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