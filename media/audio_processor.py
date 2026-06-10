"""
AudioProcessor — 语音转文字 STT（v5.1）

功能:
- 支持 Whisper API（OpenAI / 硅基流动）
- 音频格式校验（WAV/MP3/OGG/WebM）
- 大小限制
- 转写结果返回
"""


import httpx

from core import config
from core.logger import get_logger

logger = get_logger("media.audio")

# 支持的音频格式
ALLOWED_AUDIO_TYPES = [
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
    "audio/webm",
    "audio/mp4",
    "audio/m4a",
]
MAX_AUDIO_SIZE_MB = 25  # Whisper API 限制 25MB


class AudioProcessor:
    """语音转文字处理器（STT）"""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "whisper-1",
        max_size_mb: int = MAX_AUDIO_SIZE_MB,
    ):
        self.api_key = api_key or config.OPENAI_API_KEY
        self.base_url = (base_url or config.OPENAI_BASE_URL).rstrip("/")
        self.model = model
        self.max_size_mb = max_size_mb

    def validate_audio(self, data: bytes, content_type: str) -> str | None:
        """校验音频文件，返回错误信息或 None"""
        if content_type not in ALLOWED_AUDIO_TYPES:
            return f"不支持的音频格式: {content_type}"
        if len(data) > self.max_size_mb * 1024 * 1024:
            return f"音频大小超过限制（最大 {self.max_size_mb}MB）"
        if len(data) == 0:
            return "音频文件为空"
        return None

    async def transcribe(self, audio_bytes: bytes, content_type: str, language: str = "zh") -> str:
        """
        调用 Whisper API 转写语音为文字

        Args:
            audio_bytes: 音频字节数据
            content_type: MIME 类型
            language: 语言代码（默认中文）

        Returns:
            转写后的文字

        Raises:
            ValueError: 格式或大小不合法
            Exception: API 调用失败
        """
        error = self.validate_audio(audio_bytes, content_type)
        if error:
            raise ValueError(error)

        # 确定文件扩展名
        ext_map = {
            "audio/wav": "wav",
            "audio/x-wav": "wav",
            "audio/wave": "wav",
            "audio/mpeg": "mp3",
            "audio/mp3": "mp3",
            "audio/ogg": "ogg",
            "audio/webm": "webm",
            "audio/mp4": "m4a",
            "audio/m4a": "m4a",
        }
        ext = ext_map.get(content_type, "wav")

        # 调用 Whisper API
        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        files = {
            "file": (f"audio.{ext}", audio_bytes, content_type),
        }
        data = {
            "model": self.model,
            "language": language,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, files=files, data=data)
            resp.raise_for_status()
            result = resp.json()
            text = result.get("text", "")
            logger.info(f"STT 转写完成: {len(audio_bytes)} bytes → {len(text)} chars")
            return text
