"""
TTSProcessor — 文字转语音 TTS（v5.1）

功能:
- Edge TTS（免费，中文效果好，无需 API Key）
- 支持多种中文语音（zh-CN-XiaoxiaoNeural 等）
- 返回音频字节流
- issue #52: ``TTS_PROVIDER=local`` 时走进程内确定性替身，**零出网**
"""

import io

from core import config
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

        # issue #52: local provider —— 不构造 HTTP 客户端，不连 edge_tts 的
        # 公网端点（speech.platform.bing.com）。默认测试 lane 走这里。
        # 这不是"假装合成成功"：它返回的是**确定性占位音频**，内容随文本变化，
        # 让调用方的字节流分支（audio/* 响应）被真实走到；同时它明确**不是**
        # 可听的语音，所以不会被误当成 TTS 质量已验证。
        if config.TTS_PROVIDER == config.PROVIDER_LOCAL:
            audio_bytes = self._local_synthesize(text)
            logger.info(
                f"TTS 本地替身合成完成（provider=local，非真实语音）: "
                f"{len(text)} chars → {len(audio_bytes)} bytes"
            )
            return audio_bytes

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
    def _local_synthesize(text: str) -> bytes:
        """确定性占位音频（issue #52 的 local provider）。

        刻意**不是**可播放的语音 —— 它是一段确定性字节流，作用是让"拿到音频字节 →
        以audio/* 返回"这条业务链路在完全离线的默认 lane 里被真实走到。

        用 blake2b 而不是内置 hash()：跨进程必须一致（与 rag/local_provider 同一
        理由），否则同一段文本在两次请求里返回不同字节，无法作为确定性替身。
        """
        import hashlib

        payload = hashlib.blake2b(
            text.strip().encode("utf-8"), digest_size=32
        ).digest()
        return b"ID3\x03\x00\x00\x00\x00\x00\x00" + payload

    @staticmethod
    def list_voices() -> dict:
        """返回可用的中文语音列表"""
        return CHINESE_VOICES.copy()
