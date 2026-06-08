"""
ImageProcessor — 图片预处理（v5.1）

功能:
- 格式校验（JPEG/PNG/WebP/GIF）
- 尺寸压缩（长边 ≤ MAX_LONG_EDGE，保持宽高比）
- 质量压缩（JPEG/WebP quality 控制）
- base64 编码 + data URL 生成
- 大小校验（原始 + 编码后）
"""

import base64
import io
import logging
from typing import Optional, Tuple

from PIL import Image

import config

logger = logging.getLogger(__name__)

# 默认压缩参数
MAX_LONG_EDGE = 2048  # 长边最大像素
JPEG_QUALITY = 85  # JPEG 压缩质量
WEBP_QUALITY = 85  # WebP 压缩质量


class ImageProcessor:
    """图片预处理器：校验 → 压缩 → base64 编码"""

    def __init__(
        self,
        max_size_mb: int = 0,
        allowed_types: list | None = None,
        max_long_edge: int = MAX_LONG_EDGE,
        jpeg_quality: int = JPEG_QUALITY,
    ):
        self.max_size_mb = max_size_mb or config.MAX_IMAGE_SIZE_MB
        self.allowed_types = allowed_types or config.ALLOWED_IMAGE_TYPES
        self.max_long_edge = max_long_edge
        self.jpeg_quality = jpeg_quality

    def validate_mime(self, content_type: str) -> bool:
        """校验 MIME 类型是否允许"""
        return content_type in self.allowed_types

    def validate_size(self, data: bytes) -> bool:
        """校验原始文件大小"""
        return len(data) <= self.max_size_mb * 1024 * 1024

    def process(self, data: bytes, content_type: str) -> str:
        """
        完整处理流程：校验 → 压缩 → 编码 → 返回 data URL

        Args:
            data: 原始图片字节
            content_type: MIME 类型

        Returns:
            base64 data URL，如 "data:image/jpeg;base64,..."

        Raises:
            ValueError: 格式或大小不合法
        """
        if not self.validate_mime(content_type):
            raise ValueError(
                f"不支持的图片格式: {content_type}，仅支持: {', '.join(self.allowed_types)}"
            )

        if not self.validate_size(data):
            raise ValueError(
                f"图片大小超出限制: {len(data) / 1024 / 1024:.1f}MB > {self.max_size_mb}MB"
            )

        # 打开并压缩
        compressed_bytes, output_type = self._compress(data, content_type)

        # base64 编码
        b64 = base64.b64encode(compressed_bytes).decode("utf-8")
        data_url = f"data:{output_type};base64,{b64}"

        # 编码后大小校验（防止 base64 膨胀后过大，一般 15MB 上限）
        encoded_mb = len(b64) / 1024 / 1024
        if encoded_mb > 15:
            raise ValueError(f"编码后图片过大: {encoded_mb:.1f}MB > 15MB")

        logger.debug(
            "图片处理完成: %s → %s, %.1fKB", content_type, output_type, len(compressed_bytes) / 1024
        )
        return data_url

    def _compress(self, data: bytes, content_type: str) -> tuple[bytes, str]:
        """
        压缩图片：缩放长边 + 质量压缩

        Returns:
            (compressed_bytes, output_mime_type)
        """
        try:
            img = Image.open(io.BytesIO(data))
        except Exception as e:
            raise ValueError(f"无法解析图片: {e}") from e

        # 转为 RGB（去掉 alpha 通道，JPEG 不支持 RGBA）
        if img.mode in ("RGBA", "LA", "P") or img.mode != "RGB":
            img = img.convert("RGB")
            output_type = "image/jpeg"
        else:
            output_type = content_type

        # 缩放（保持宽高比，长边不超过 max_long_edge）
        w, h = img.size
        if max(w, h) > self.max_long_edge:
            ratio = self.max_long_edge / max(w, h)
            new_w, new_h = int(w * ratio), int(h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            logger.debug("图片缩放: %dx%d → %dx%d", w, h, new_w, new_h)

        # 写入字节流
        buf = io.BytesIO()
        fmt = (
            "JPEG"
            if output_type == "image/jpeg"
            else "WEBP"
            if output_type == "image/webp"
            else "PNG"
        )
        save_kwargs = {}
        if fmt == "JPEG":
            save_kwargs = {"quality": self.jpeg_quality, "optimize": True}
        elif fmt == "WEBP":
            save_kwargs = {"quality": WEBP_QUALITY}

        img.save(buf, format=fmt, **save_kwargs)
        return buf.getvalue(), output_type

    @staticmethod
    def get_info(data: bytes, content_type: str) -> dict:
        """获取图片基本信息（不压缩）"""
        try:
            img = Image.open(io.BytesIO(data))
            return {
                "width": img.size[0],
                "height": img.size[1],
                "format": img.format or content_type,
                "mode": img.mode,
                "size_bytes": len(data),
                "size_mb": round(len(data) / 1024 / 1024, 2),
            }
        except Exception:
            return {"size_bytes": len(data), "size_mb": round(len(data) / 1024 / 1024, 2)}
