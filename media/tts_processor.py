"""
TTSProcessor — 文字转语音 TTS（v5.1）

功能:
- Edge TTS（免费，中文效果好，无需 API Key）
- 支持多种中文语音（zh-CN-XiaoxiaoNeural 等）
- 返回音频字节流
"""

import io

from core.logger import get_logger

logger = get_logger("media.tts")

# 默认中文语音
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# 可用中文语音列表
CHINESE_VOICES = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",  # 女声（默认，温暖自然）
    "yunxi": "zh-CN-YunxiNeural",  # 男声（年轻）
    "yunjian": "zh-CN-YunjianNeural",  # 男声（成熟）
    "xiaoyi": "zh-CN-XiaoyiNeural",  # 女声（活泼）
}


class TTSProcessor:
    """文字转语音处理器（使用 Edge TTS）"""

    def __init__(self, voice: str = "", rate: str = "+0%", volume: str = "+0%"):
        self.voice = voice or DEFAULT_VOICE
        self.rate = rate
        self.volume = volume

    async def synthesize(self, text: str, voice: str = "") -> bytes:
        """
        将文字转为语音音频（MP3 格式）

        Args:
            text: 要转换的文字
            voice: 语音名称（可选，覆盖默认）

        Returns:
            MP3 音频字节数据

        Raises:
            ValueError: 文本为空
            ImportError: edge_tts 未安装
            Exception: TTS 调用失败
        """
        if not text or not text.strip():
            raise ValueError("文本不能为空")

        try:
            import edge_tts
        except ImportError as e:
            raise ImportError("edge_tts 未安装，请运行: pip install edge-tts") from e

        voice_name = voice or self.voice
        # 截断过长文本（防止超时）
        if len(text) > 2000:
            text = text[:2000] + "..."
            logger.warning("TTS 文本截断至 2000 字符")

        buf = io.BytesIO()
        communicate = edge_tts.Communicate(text, voice_name, rate=self.rate, volume=self.volume)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])

        audio_bytes = buf.getvalue()
        logger.info(f"TTS 合成完成: {len(text)} chars → {len(audio_bytes)} bytes")
        return audio_bytes

    @staticmethod
    def list_voices() -> dict:
        """返回可用的中文语音列表"""
        return CHINESE_VOICES.copy()
