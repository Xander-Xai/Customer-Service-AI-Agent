"""
DocumentProcessor — 文档内容提取（v5.1）

功能:
- PDF 文本提取（pdfplumber）
- Word 文档提取（python-docx）
- 纯文本/Markdown 直接读取
- 大小限制 + 内容截断
"""

import logging
from typing import Optional

from logger import get_logger

logger = get_logger("media.document")

ALLOWED_DOC_TYPES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
]
MAX_DOC_SIZE_MB = 10
MAX_CONTENT_LENGTH = 10000  # 提取内容最大字符数


class DocumentProcessor:
    """文档内容提取器"""

    def __init__(
        self,
        max_size_mb: int = MAX_DOC_SIZE_MB,
        max_content_length: int = MAX_CONTENT_LENGTH,
    ):
        self.max_size_mb = max_size_mb
        self.max_content_length = max_content_length

    def validate(self, data: bytes, content_type: str) -> str | None:
        """校验文档文件"""
        if content_type not in ALLOWED_DOC_TYPES:
            return f"不支持的文档格式: {content_type}，支持: PDF/DOCX/TXT/MD"
        if len(data) > self.max_size_mb * 1024 * 1024:
            return f"文档大小超过限制（最大 {self.max_size_mb}MB）"
        if len(data) == 0:
            return "文档文件为空"
        return None

    def extract(self, data: bytes, content_type: str, filename: str = "") -> str:
        """
        提取文档文本内容

        Args:
            data: 文档字节数据
            content_type: MIME 类型
            filename: 文件名（用于判断 .md 文件）

        Returns:
            提取的文本内容（截断至 max_content_length）

        Raises:
            ValueError: 格式或大小不合法
        """
        error = self.validate(data, content_type)
        if error:
            raise ValueError(error)

        if content_type == "application/pdf":
            text = self._extract_pdf(data)
        elif (
            content_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ):
            text = self._extract_docx(data)
        elif content_type in ("text/plain", "text/markdown"):
            text = self._extract_text(data)
        else:
            raise ValueError(f"未支持的文档类型: {content_type}")

        # 截断
        if len(text) > self.max_content_length:
            text = text[: self.max_content_length] + f"\n\n... [内容截断，共 {len(text)} 字符]"

        logger.info(f"文档提取完成: {content_type} → {len(text)} chars")
        return text

    def _extract_pdf(self, data: bytes) -> str:
        """提取 PDF 文本"""
        try:
            import io

            import pdfplumber

            text_parts = []
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
            return "\n\n".join(text_parts) if text_parts else "[PDF 文件无可提取的文本内容]"
        except ImportError:
            raise ImportError("pdfplumber 未安装，请运行: pip install pdfplumber")
        except Exception as e:
            logger.error(f"PDF 提取失败: {e}")
            return f"[PDF 提取失败: {e}]"

    def _extract_docx(self, data: bytes) -> str:
        """提取 Word 文档文本"""
        try:
            import io

            from docx import Document

            doc = Document(io.BytesIO(data))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n\n".join(paragraphs) if paragraphs else "[Word 文档无可提取的文本内容]"
        except ImportError:
            raise ImportError("python-docx 未安装，请运行: pip install python-docx")
        except Exception as e:
            logger.error(f"DOCX 提取失败: {e}")
            return f"[DOCX 提取失败: {e}]"

    def _extract_text(self, data: bytes) -> str:
        """提取纯文本/Markdown"""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return data.decode("gbk")
            except UnicodeDecodeError:
                return data.decode("latin-1")
