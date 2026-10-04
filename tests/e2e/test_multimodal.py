"""
多模态输入链路端到端测试（issue #52：默认 lane 必须离线 + deterministic）

覆盖：
  - /api/chat/multimodal 统一多模态入口（含 file_type header；image / voice /
    无文件 / 不支持格式）
  - /api/tts/voices      TTS 语音列表
  - /api/tts             TTS 合成

离线契约
--------
`make test` 对外承诺「离线、无需 API Key」。这条链路上有两个**第三方传输**是真实
网络调用：

  1. ``media.audio_processor.AudioProcessor.transcribe`` → ``{OPENAI_BASE_URL}/audio/transcriptions``
     （``.env.test`` 下即 ``api.siliconflow.cn``）
  2. ``media.tts_processor.TTSProcessor.synthesize`` → edge-tts（``speech.platform.bing.com``）

默认 lane 把这两个**传输层**换成确定性 double，其余全部真实执行：路由分支、文件
校验、``ImageProcessor`` / ``AudioProcessor`` 的解析逻辑、``run_graph`` 都跑真代码。
vision / LLM 在离线 lane 下由规则引擎兜底，本身不发 HTTP。

断言不是「四个状态码里随便落一个」，而是钉住这条 lane 上**确定**的行为：
image / voice 必须 200（之前之所以落 500，是因为测试自带的「1x1 PNG」其实是坏的
字节流，``ImageProcessor`` 直接报 broken data stream）；TTS 必须回放 double 的
字节。

真实 provider 的端到端验证放在文件末尾的 ``TestRealProviderMedia``：``real_llm``
标记 + 真实 key 守卫，不在默认 lane 执行。
"""

from __future__ import annotations

import base64
import io
import os
from unittest.mock import patch

import pytest

# 一个真实可解码的 2x2 红图（PIL 生成，compress_level=1，73 字节）。
# 之前这里是一段手写的、语法非法的 PNG 字节流：ImageProcessor 抛
# "broken data stream when reading image file"，端点返回 500，而断言写成
# `status_code in (200, 422, 500, 503)` —— 于是测试「通过」了，却什么都没验。
_VALID_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4AWP8zwACTGCSAQANHQEDdCJsBwAAAABJRU5ErkJggg=="

#: ASR double 回放的转写文本（确定性）。
_TRANSCRIPT = "我想了解烟酰胺精华适合什么肤质"
#: TTS double 回放的音频字节。**不是**可解码的真实音频，是一个以 ID3 tag 开头的
#: 确定性合成负载 —— 这里验的是「端点把 provider 的字节原样透传出来」。
_FAKE_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


def _make_image_bytes() -> io.BytesIO:
    """一个**可解码**的 PNG 字节流（见 _VALID_PNG_B64 的注释）。"""
    return io.BytesIO(base64.b64decode(_VALID_PNG_B64))


def _make_audio_bytes() -> io.BytesIO:
    """一个最小 WAV 音频字节流（44 字节头 + 静默采样）。"""
    return io.BytesIO(bytes(44))


# ===== 第三方传输的确定性 double =====


class _FakeHTTPResponse:
    """最小 httpx.Response 替身：只需 json() / raise_for_status()。"""

    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _OfflineASRClient:
    """``httpx.AsyncClient`` 替身，只服务 Whisper/STT 传输。

    刻意做成「白名单 + 响亮失败」：URL 不是 ``/audio/transcriptions`` 就直接
    assert 失败。这样一旦默认 lane 又冒出别的出网调用，是这一条用例先报错，
    而不是悄悄跑一次真实请求。
    """

    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        self._args = args
        self._kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, **kwargs):
        type(self).calls.append(url)
        assert "/audio/transcriptions" in url, (
            f"默认离线 lane 不应访问 {url}；只允许 STT 传输被 double 接管"
        )
        assert isinstance(self._kwargs.get("trust_env"), bool)
        return _FakeHTTPResponse({"text": _TRANSCRIPT})


class _FakeCommunicate:
    """``edge_tts.Communicate`` 替身，回放固定字节。"""

    calls: list[tuple[str, str]] = []

    def __init__(self, text: str, voice: str, **kwargs):
        self._text = text
        self._voice = voice
        type(self).calls.append((text, voice))

    async def stream(self):
        yield {"type": "audio", "data": _FAKE_MP3}


@pytest.fixture
def offline_media_transports():
    """接管两个第三方 provider 传输，并暴露调用记录供断言。

    注意：patch 的是 ``httpx.AsyncClient`` 这个类属性（``media.audio_processor``
    是 ``import httpx`` 后按属性访问的，没法只 patch 单个模块的命名空间）。
    因此 double 内部对 URL 做白名单校验 —— 任何**其它**出网都会在这里炸。
    """
    _OfflineASRClient.calls = []
    _FakeCommunicate.calls = []
    edge_tts = pytest.importorskip("edge_tts")
    with (
        patch("httpx.AsyncClient", _OfflineASRClient),
        patch.object(edge_tts, "Communicate", _FakeCommunicate),
    ):
        yield {"asr": _OfflineASRClient, "tts": _FakeCommunicate}


def _client():
    from fastapi.testclient import TestClient

    from api.app_factory import app

    return TestClient(app)


# ===== /api/chat/multimodal =====


@pytest.mark.asyncio
async def test_multimodal_endpoint_image(offline_media_transports):
    """图片入口：合法 PNG → 200 + type=image + data URL 透传。

    断言是确定行为而不是「几个状态码都行」：图片链路在离线 lane 下必须成功，
    因为它不依赖任何 provider —— 只有 vision 侧的 LLM 会由规则引擎兜底。
    """
    with _client() as client:
        response = client.post(
            "/api/chat/multimodal",
            files={"file": ("test.png", _make_image_bytes(), "image/png")},
            data={"message": "这张图里有什么？"},
            headers={"X-File-Type": "image"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["type"] == "image"
        assert data["image_url"].startswith("data:image/png;base64,")
        assert data["message"] == "这张图里有什么？"
        assert isinstance(data["response"], str) and data["response"]
        assert data["agent"]
        # 图片链路不碰任何 provider 传输
        assert offline_media_transports["asr"].calls == []
        assert offline_media_transports["tts"].calls == []


@pytest.mark.asyncio
async def test_multimodal_endpoint_voice(offline_media_transports):
    """语音入口：STT double 返回转写 → 200 + type=voice + 转写透传。"""
    with _client() as client:
        audio_bytes = _make_audio_bytes()
        response = client.post(
            "/api/chat/multimodal",
            files={"file": ("test.wav", audio_bytes, "audio/wav")},
            data={"message": ""},
            headers={"X-File-Type": "voice"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["type"] == "voice"
        assert data["transcription"] == _TRANSCRIPT
        assert data["message"] == _TRANSCRIPT
        assert isinstance(data["response"], str) and data["response"]

    # STT 传输确实被调用过（否则 200 只是巧合）
    stt_calls = offline_media_transports["asr"].calls
    assert len(stt_calls) == 1 and "/audio/transcriptions" in stt_calls[0]


@pytest.mark.asyncio
async def test_multimodal_endpoint_no_file(offline_media_transports):
    """无文件：返回纯文本引导，不触发任何 provider。"""
    with _client() as client:
        response = client.post("/api/chat/multimodal", data={"message": "你好"})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["type"] == "text"
        assert data["message"] == "你好"
        assert data["note"]


@pytest.mark.asyncio
async def test_multimodal_endpoint_unsupported_file(offline_media_transports):
    """不支持的格式：400 且错误里回显 content type。"""
    with _client() as client:
        response = client.post(
            "/api/chat/multimodal",
            files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")},
            data={"message": ""},
        )
        assert response.status_code == 400, response.text
        assert "text/plain" in response.json()["error"]


# ===== TTS =====


@pytest.mark.asyncio
async def test_tts_voices_endpoint(offline_media_transports):
    """TTS 语音列表端点：本地静态表，不出网。"""
    with _client() as client:
        response = client.get("/api/tts/voices")
        assert response.status_code == 200, response.text
        voices = response.json()["voices"]
        assert voices["xiaoxiao"] == "zh-CN-XiaoxiaoNeural"
        assert set(voices) == {"xiaoxiao", "yunxi", "yunjian", "xiaoyi"}
    assert offline_media_transports["tts"].calls == []


@pytest.mark.asyncio
async def test_tts_endpoint(offline_media_transports):
    """TTS 合成：端点必须把 provider 字节原样透传（audio/mpeg + 完整 body）。"""
    with _client() as client:
        response = client.post("/api/tts", json={"text": "你好，欢迎咨询", "voice": ""})
        assert response.status_code == 200, response.text
        assert response.headers.get("content-type", "").startswith("audio/")
        assert response.content == _FAKE_MP3

    calls = offline_media_transports["tts"].calls
    assert calls == [("你好，欢迎咨询", "zh-CN-XiaoxiaoNeural")]


@pytest.mark.asyncio
async def test_tts_endpoint_empty_text(offline_media_transports):
    """TTS 空文本：400，且不触发合成（也就没有出网）。"""
    with _client() as client:
        response = client.post("/api/tts", json={"text": "", "voice": ""})
        assert response.status_code == 400
        assert "error" in response.json()
    assert offline_media_transports["tts"].calls == []


# ===== 真实 provider lane（不在默认 lane 执行）=====


def _real_provider_configured() -> bool:
    """与 tests/e2e/test_e2e_real_llm.py 同款的真实 key 判定。

    占位 key（``.env.test`` 的 ``sk-placeholder-*``）一律不算 —— 否则这条 lane
    会在默认测试里拿着假凭据去打真 provider，重现 issue #52。
    """
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return False
    if any(
        key.lower().startswith(p)
        for p in ("your-", "sk-placeholder", "sk-xxx", "sk-your", "sk-test")
    ):
        return False
    return os.environ.get("LLM_PROVIDER", "siliconflow") in ("openai", "custom")


@pytest.mark.skipif(
    not _real_provider_configured(),
    reason="无真实 provider 凭据（占位 key 不算），跳过真实多模态传输测试",
)
@pytest.mark.real_llm
class TestRealProviderMedia:
    """真实 STT / TTS 传输验证。默认 lane 用 double 覆盖，这里保留真实链路。

    运行方式：``EMBEDDING_PROVIDER=api`` + 真实 provider 凭据，然后
    ``pytest tests/e2e/test_multimodal.py -m real_llm``。
    """

    @pytest.mark.asyncio
    async def test_real_tts_returns_audio(self):
        edge_tts = pytest.importorskip("edge_tts")
        assert hasattr(edge_tts, "Communicate")
        with _client() as client:
            response = client.post("/api/tts", json={"text": "语音合成测试", "voice": ""})
        assert response.status_code == 200, response.text
        assert response.headers.get("content-type", "").startswith("audio/")
        assert len(response.content) > 0

    @pytest.mark.asyncio
    async def test_real_asr_transcribes_audio(self):
        with _client() as client:
            response = client.post(
                "/api/chat/multimodal",
                files={"file": ("test.wav", _make_audio_bytes(), "audio/wav")},
                data={"message": ""},
                headers={"X-File-Type": "voice"},
            )
        # 44 字节的空 WAV 不是真实语音：真实 provider 会拒绝它（400/500），
        # 关键是**没有**被 double 接管 —— 请求真的发出去了。
        assert response.status_code in (200, 400, 500), response.text
        assert "transcription" in response.text or "error" in response.text
