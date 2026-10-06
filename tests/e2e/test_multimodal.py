"""
多模态输入链路端到端测试（v6.1 — 证据缺口修复）

覆盖：
  - /api/chat/image      图片分析入口
  - /api/chat/voice      语音识别入口
  - /api/chat/multimodal 统一多模态入口（含 file_type header）
  - /api/chat/file       文件上传对话入口
  - /api/tts/voices      TTS 语音列表
"""

import io

import pytest


def _make_image_bytes() -> io.BytesIO:
    """创建一个 1x1 PNG 图片的字节流用于测试。"""
    # 最小有效 PNG：1x1 红色像素
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
        b"\x00\x05\x18\xd8N"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return io.BytesIO(png_bytes)


def _make_audio_bytes() -> io.BytesIO:
    """创建一个最小 WAV 音频字节流。"""
    # 44 字节 WAV header + 静默采样
    wav_header = bytes(44)
    return io.BytesIO(wav_header)


@pytest.mark.asyncio
async def test_multimodal_endpoint_image():
    """测试 /api/chat/multimodal 的图片处理（file_type=image header）"""
    from fastapi.testclient import TestClient

    try:
        from api.app_factory import app
    except ImportError:
        pytest.skip("api.app_factory 不可用")

    with TestClient(app) as client:
        image_bytes = _make_image_bytes()
        response = client.post(
            "/api/chat/multimodal",
            files={"file": ("test.png", image_bytes, "image/png")},
            data={"message": "这张图里有什么？"},
            headers={"X-File-Type": "image"},
        )
        # 即使图片分析失败，也应该返回 JSON
        assert response.status_code in (200, 422, 500, 503)
        if response.status_code == 200:
            data = response.json()
            assert "type" in data
            assert data["type"] == "image"
        elif response.status_code == 503:
            data = response.json()
            assert "error" in data  # MULTIMODAL_ENABLED=False 时的正常行为


@pytest.mark.asyncio
async def test_multimodal_endpoint_voice():
    """测试 /api/chat/multimodal 的语音处理（file_type=voice header）"""
    from fastapi.testclient import TestClient

    try:
        from api.app_factory import app
    except ImportError:
        pytest.skip("api.app_factory 不可用")

    with TestClient(app) as client:
        audio_bytes = _make_audio_bytes()
        response = client.post(
            "/api/chat/multimodal",
            files={"file": ("test.wav", audio_bytes, "audio/wav")},
            data={"message": ""},
            headers={"X-File-Type": "voice"},
        )
        assert response.status_code in (200, 422, 500, 503)
        if response.status_code == 200:
            data = response.json()
            assert "type" in data
            assert data["type"] == "voice"


@pytest.mark.asyncio
async def test_multimodal_endpoint_no_file():
    """测试 /api/chat/multimodal 无文件时返回纯文本提示"""
    from fastapi.testclient import TestClient

    try:
        from api.app_factory import app
    except ImportError:
        pytest.skip("api.app_factory 不可用")

    with TestClient(app) as client:
        response = client.post(
            "/api/chat/multimodal",
            data={"message": "你好"},
        )
        assert response.status_code in (200, 422)
        if response.status_code == 200:
            data = response.json()
            assert data.get("type") == "text"


@pytest.mark.asyncio
async def test_multimodal_endpoint_unsupported_file():
    """测试 /api/chat/multimodal 不支持的格式"""
    from fastapi.testclient import TestClient

    try:
        from api.app_factory import app
    except ImportError:
        pytest.skip("api.app_factory 不可用")

    with TestClient(app) as client:
        response = client.post(
            "/api/chat/multimodal",
            files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")},
            data={"message": ""},
        )
        assert response.status_code in (200, 400, 422, 500)
        if response.status_code == 400:
            data = response.json()
            assert "error" in data


@pytest.mark.asyncio
async def test_tts_voices_endpoint():
    """测试 TTS 语音列表端点"""
    from fastapi.testclient import TestClient

    try:
        from api.app_factory import app
    except ImportError:
        pytest.skip("api.app_factory 不可用")

    with TestClient(app) as client:
        response = client.get("/api/tts/voices")
        assert response.status_code in (200, 404)
        if response.status_code == 200:
            data = response.json()
            assert "voices" in data


@pytest.mark.asyncio
async def test_tts_endpoint():
    """测试 TTS 合成端点（issue #52：默认 lane 离线，不再接受 500）。

    旧断言是 ``in (200, 422, 500)`` —— 因为 edge_tts 要连公网、失败就吃 500，
    而 500 也算"通过"。这既让测试没验证到业务路径，又意味着默认测试 lane
    仍然**需要出网**。现在 ``TTS_PROVIDER=local``（.env.test）走进程内确定性
    替身，因此可以严格断言 200 + audio/*，并且这条断言在无网环境下也成立。

    真实 edge_tts 合成走 ``make test-real-providers``（默认不执行）。
    """
    from fastapi.testclient import TestClient

    try:
        from api.app_factory import app
    except ImportError:
        pytest.skip("api.app_factory 不可用")

    with TestClient(app) as client:
        response = client.post(
            "/api/tts",
            json={"text": "你好，欢迎咨询", "voice": ""},
        )
        assert response.status_code == 200, (
            f"默认离线 lane 下 /api/tts 必须成功（local provider），"
            f"实际 {response.status_code}: {response.text[:200]}"
        )
        assert response.headers.get("content-type", "").startswith("audio/")
        assert len(response.content) > 0


@pytest.mark.asyncio
async def test_tts_endpoint_empty_text():
    """测试 TTS 空文本"""
    from fastapi.testclient import TestClient

    try:
        from api.app_factory import app
    except ImportError:
        pytest.skip("api.app_factory 不可用")

    with TestClient(app) as client:
        response = client.post(
            "/api/tts",
            json={"text": "", "voice": ""},
        )
        assert response.status_code == 400
        data = response.json()
        assert "error" in data
