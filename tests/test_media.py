"""
media 包测试（v5.1）
覆盖：image_processor / audio_processor / video_processor / document_processor / tts_processor

运行: pytest tests/test_media.py -v --tb=short
"""

import base64
import io
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ─────────────────────────────────────────────────────────────────────────────
# Helper: 用 PIL 生成一个小型 JPEG 图片的字节
# ─────────────────────────────────────────────────────────────────────────────


def _make_test_jpeg(width: int = 100, height: int = 80) -> bytes:
    """生成一个简单的小 JPEG 图片（纯色填充）"""
    from PIL import Image

    img = Image.new("RGB", (width, height), color=(128, 64, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_test_png(width: int = 100, height: int = 80) -> bytes:
    """生成一个简单的小 PNG 图片"""
    from PIL import Image

    img = Image.new("RGBA", (width, height), color=(128, 64, 200, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. ImageProcessor
# ═══════════════════════════════════════════════════════════════════════════════


class TestImageProcessor:
    """ImageProcessor 全面验证"""

    def test_process_valid_jpeg_returns_data_url(self):
        """process() 处理合法 JPEG 返回 data:image/jpeg;base64,... 格式"""
        from media.image_processor import ImageProcessor

        proc = ImageProcessor()
        jpeg_data = _make_test_jpeg()
        result = proc.process(jpeg_data, "image/jpeg")

        assert result.startswith("data:image/jpeg;base64,")
        # 确保 base64 部分可解码
        b64_part = result.split(",", 1)[1]
        decoded = base64.b64decode(b64_part)
        assert len(decoded) > 0

    def test_process_valid_png_returns_data_url(self):
        """process() 处理合法 PNG 返回 data:image/jpeg;base64,...（RGBA 转 JPEG）"""
        from media.image_processor import ImageProcessor

        proc = ImageProcessor()
        png_data = _make_test_png()
        result = proc.process(png_data, "image/png")

        # RGBA PNG 会转为 JPEG
        assert result.startswith("data:image/jpeg;base64,")

    def test_process_unsupported_mime_raises(self):
        """process() 不支持的 MIME 类型抛出 ValueError"""
        from media.image_processor import ImageProcessor

        proc = ImageProcessor()
        jpeg_data = _make_test_jpeg()

        with pytest.raises(ValueError, match="不支持的图片格式"):
            proc.process(jpeg_data, "image/bmp")

    def test_process_oversized_data_raises(self):
        """process() 超出大小限制抛出 ValueError"""
        from media.image_processor import ImageProcessor

        # max_size_mb 使用 "or" 语句，0 会被替换为 config 默认值，所以用 0.000001
        tiny_limit_proc = ImageProcessor(max_size_mb=0.000001, max_long_edge=1024, jpeg_quality=50)
        jpeg_data = _make_test_jpeg()
        with pytest.raises(ValueError, match="图片大小超出限制"):
            tiny_limit_proc.process(jpeg_data, "image/jpeg")

    def test_process_invalid_image_data_raises(self):
        """process() 非法图片数据抛出 ValueError"""
        from media.image_processor import ImageProcessor

        proc = ImageProcessor(max_size_mb=5)
        with pytest.raises(ValueError, match="无法解析图片"):
            proc.process(b"this is not an image", "image/jpeg")

    def test_validate_mime_accepts_allowed_types(self):
        """validate_mime() 接受允许的 MIME 类型"""
        from media.image_processor import ImageProcessor

        proc = ImageProcessor()
        assert proc.validate_mime("image/jpeg") is True
        assert proc.validate_mime("image/png") is True
        assert proc.validate_mime("image/webp") is True

    def test_validate_mime_rejects_disallowed_types(self):
        """validate_mime() 拒绝不允许的 MIME 类型"""
        from media.image_processor import ImageProcessor

        proc = ImageProcessor()
        assert proc.validate_mime("image/bmp") is False
        assert proc.validate_mime("image/tiff") is False
        assert proc.validate_mime("application/octet-stream") is False

    def test_validate_size_within_limit(self):
        """validate_size() 小于限制返回 True"""
        from media.image_processor import ImageProcessor

        proc = ImageProcessor(max_size_mb=5)
        assert proc.validate_size(b"\x00" * 100) is True

    def test_validate_size_exceeds_limit(self):
        """validate_size() 超过限制返回 False"""
        from media.image_processor import ImageProcessor

        # 注意: max_size_mb 使用 "or" 语句，0 为 falsy 会回退到 config 默认值
        # 所以用一个极小的正值来测试
        proc = ImageProcessor(max_size_mb=0.000001)
        assert proc.validate_size(b"\x00" * 10) is False

    def test_get_info_returns_dimensions(self):
        """get_info() 返回图片宽高和格式"""
        from media.image_processor import ImageProcessor

        jpeg_data = _make_test_jpeg(120, 90)
        info = ImageProcessor.get_info(jpeg_data, "image/jpeg")

        assert info["width"] == 120
        assert info["height"] == 90
        assert "format" in info
        assert "size_bytes" in info
        assert info["size_bytes"] == len(jpeg_data)

    def test_get_info_invalid_data_returns_minimal(self):
        """get_info() 非法数据返回最小信息"""
        from media.image_processor import ImageProcessor

        info = ImageProcessor.get_info(b"not an image", "image/jpeg")

        assert "width" not in info
        assert info["size_bytes"] == 12

    def test_process_large_image_gets_resized(self):
        """process() 超大图片会被缩放至 max_long_edge 以内"""
        from media.image_processor import ImageProcessor

        proc = ImageProcessor(max_long_edge=200, max_size_mb=5)
        large_jpeg = _make_test_jpeg(1000, 800)
        result = proc.process(large_jpeg, "image/jpeg")

        assert result.startswith("data:image/jpeg;base64,")
        # 解码后检查尺寸（通过 get_info）
        b64_part = result.split(",", 1)[1]
        decoded = base64.b64decode(b64_part)
        info = ImageProcessor.get_info(decoded, "image/jpeg")
        assert info["width"] <= 200
        assert info["height"] <= 200

    def test_custom_allowed_types(self):
        """自定义 allowed_types 覆盖默认值"""
        from media.image_processor import ImageProcessor

        proc = ImageProcessor(allowed_types=["image/gif"])
        assert proc.validate_mime("image/gif") is True
        assert proc.validate_mime("image/jpeg") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. AudioProcessor
# ═══════════════════════════════════════════════════════════════════════════════


class TestAudioProcessor:
    """AudioProcessor 全面验证"""

    def test_validate_audio_valid(self):
        """validate_audio() 合法音频返回 None"""
        from media.audio_processor import AudioProcessor

        proc = AudioProcessor(api_key="test-key")
        error = proc.validate_audio(b"\x00" * 100, "audio/wav")
        assert error is None

    def test_validate_audio_unsupported_format(self):
        """validate_audio() 不支持的格式返回错误信息"""
        from media.audio_processor import AudioProcessor

        proc = AudioProcessor(api_key="test-key")
        error = proc.validate_audio(b"\x00" * 100, "audio/flac")
        assert "不支持的音频格式" in error

    def test_validate_audio_empty_data(self):
        """validate_audio() 空数据返回错误信息"""
        from media.audio_processor import AudioProcessor

        proc = AudioProcessor(api_key="test-key")
        error = proc.validate_audio(b"", "audio/wav")
        assert "音频文件为空" in error

    def test_validate_audio_oversized(self):
        """validate_audio() 超大文件返回错误信息"""
        from media.audio_processor import AudioProcessor

        proc = AudioProcessor(api_key="test-key", max_size_mb=1)
        big_data = b"\x00" * (2 * 1024 * 1024)  # 2MB
        error = proc.validate_audio(big_data, "audio/wav")
        assert "音频大小超过限制" in error

    def test_validate_audio_all_allowed_types(self):
        """validate_audio() 所有允许的类型都返回 None"""
        from media.audio_processor import ALLOWED_AUDIO_TYPES, AudioProcessor

        proc = AudioProcessor(api_key="test-key")
        for mime in ALLOWED_AUDIO_TYPES:
            error = proc.validate_audio(b"\x00" * 10, mime)
            assert error is None, f"{mime} should be allowed"

    @pytest.mark.asyncio
    async def test_transcribe_invalid_format_raises(self):
        """transcribe() 不支持的格式抛出 ValueError"""
        from media.audio_processor import AudioProcessor

        proc = AudioProcessor(api_key="test-key")
        with pytest.raises(ValueError, match="不支持的音频格式"):
            await proc.transcribe(b"\x00" * 10, "audio/flac")

    @pytest.mark.asyncio
    async def test_transcribe_empty_audio_raises(self):
        """transcribe() 空数据抛出 ValueError"""
        from media.audio_processor import AudioProcessor

        proc = AudioProcessor(api_key="test-key")
        with pytest.raises(ValueError, match="音频文件为空"):
            await proc.transcribe(b"", "audio/wav")

    @pytest.mark.asyncio
    async def test_transcribe_success(self):
        """transcribe() 正常调用返回转写文字"""
        from media.audio_processor import AudioProcessor

        proc = AudioProcessor(api_key="test-key", base_url="https://api.example.com/v1")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"text": "你好世界"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("media.audio_processor.httpx.AsyncClient", return_value=mock_client):
            result = await proc.transcribe(b"\x00" * 100, "audio/wav", language="zh")

        assert result == "你好世界"
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_transcribe_api_failure_raises(self):
        """transcribe() API 调用失败时抛出异常"""
        import httpx

        from media.audio_processor import AudioProcessor

        proc = AudioProcessor(api_key="test-key", base_url="https://api.example.com/v1")

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error", request=MagicMock(), response=MagicMock(status_code=500)
        )

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("media.audio_processor.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                await proc.transcribe(b"\x00" * 100, "audio/wav")

    @pytest.mark.asyncio
    async def test_transcribe_mp3_ext_mapping(self):
        """transcribe() MP3 格式正确映射文件扩展名"""
        from media.audio_processor import AudioProcessor

        proc = AudioProcessor(api_key="test-key", base_url="https://api.example.com/v1")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"text": "测试"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("media.audio_processor.httpx.AsyncClient", return_value=mock_client):
            await proc.transcribe(b"\x00" * 50, "audio/mpeg")

        # 检查调用参数中的文件名扩展名
        call_args = mock_client.post.call_args
        files_arg = call_args.kwargs.get("files") or call_args[1].get("files")
        file_tuple = files_arg["file"]
        assert file_tuple[0] == "audio.mp3"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. VideoProcessor
# ═══════════════════════════════════════════════════════════════════════════════


class TestVideoProcessor:
    """VideoProcessor 全面验证"""

    def test_validate_valid_mp4(self):
        """validate() 合法 MP4 数据返回 None"""
        from media.video_processor import VideoProcessor

        proc = VideoProcessor()
        error = proc.validate(b"\x00" * 100, "video/mp4")
        assert error is None

    def test_validate_unsupported_format(self):
        """validate() 不支持的格式返回错误信息"""
        from media.video_processor import VideoProcessor

        proc = VideoProcessor()
        error = proc.validate(b"\x00" * 100, "video/avi")
        assert "不支持的视频格式" in error

    def test_validate_empty_data(self):
        """validate() 空数据返回错误信息"""
        from media.video_processor import VideoProcessor

        proc = VideoProcessor()
        error = proc.validate(b"", "video/mp4")
        assert "视频文件为空" in error

    def test_validate_oversized(self):
        """validate() 超大文件返回错误信息"""
        from media.video_processor import VideoProcessor

        proc = VideoProcessor(max_size_mb=1)
        big_data = b"\x00" * (2 * 1024 * 1024)
        error = proc.validate(big_data, "video/mp4")
        assert "视频大小超过限制" in error

    def test_validate_all_allowed_types(self):
        """validate() 所有允许的类型都返回 None"""
        from media.video_processor import ALLOWED_VIDEO_TYPES, VideoProcessor

        proc = VideoProcessor()
        for mime in ALLOWED_VIDEO_TYPES:
            error = proc.validate(b"\x00" * 10, mime)
            assert error is None, f"{mime} should be allowed"

    def test_extract_frames_invalid_format_raises(self):
        """extract_frames() 不支持的格式抛出 ValueError"""
        from media.video_processor import VideoProcessor

        proc = VideoProcessor()
        with pytest.raises(ValueError, match="不支持的视频格式"):
            proc.extract_frames(b"\x00" * 100, "video/avi")

    def test_extract_frames_empty_data_raises(self):
        """extract_frames() 空数据抛出 ValueError"""
        from media.video_processor import VideoProcessor

        proc = VideoProcessor()
        with pytest.raises(ValueError, match="视频文件为空"):
            proc.extract_frames(b"", "video/mp4")

    def test_extract_frames_import_error(self):
        """extract_frames() cv2 未安装时抛出 ImportError"""
        from media.video_processor import VideoProcessor

        proc = VideoProcessor()
        fake_video = b"\x00" * 100

        with patch.dict("sys.modules", {"cv2": None, "numpy": None}):
            with pytest.raises(ImportError, match="opencv-python-headless 未安装"):
                proc.extract_frames(fake_video, "video/mp4")

    def test_extract_frames_with_mock_cv2(self):
        """extract_frames() 使用 mock cv2 正常提取帧"""
        from media.video_processor import VideoProcessor

        proc = VideoProcessor(max_frames=2, interval_sec=1.0)

        # 构建 mock cv2 模块
        mock_cv2 = MagicMock()
        mock_cv2.CAP_PROP_FPS = 5
        mock_cv2.CAP_PROP_FRAME_COUNT = 7
        mock_cv2.CAP_PROP_POS_FRAMES = 1
        mock_cv2.IMWRITE_JPEG_QUALITY = 2

        # 模拟 2 帧
        fake_frame = MagicMock()
        fake_frame.shape = (100, 100, 3)

        mock_cap = MagicMock()
        mock_cap.isOpened.side_effect = [True, True, False]
        mock_cap.read.side_effect = [(True, fake_frame), (True, fake_frame), (False, None)]
        mock_cap.get.side_effect = lambda prop: {5: 25.0, 7: 50}.get(prop, 0)
        mock_cap.release = MagicMock()

        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.resize.return_value = fake_frame

        # 模拟 imencode 返回包含真实 bytes 的 buffer
        fake_jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        fake_jpeg_buf = MagicMock()
        fake_jpeg_buf.tobytes.return_value = fake_jpeg_bytes
        # base64.b64encode() 需要 iter(bytes) 所以用真实 bytes
        mock_cv2.imencode.return_value = (True, fake_jpeg_bytes)

        mock_np = MagicMock()

        with patch.dict("sys.modules", {"cv2": mock_cv2, "numpy": mock_np}):
            frames = proc.extract_frames(b"\x00" * 100, "video/mp4")

        assert len(frames) <= 2
        assert len(frames) > 0
        assert all(f.startswith("data:image/jpeg;base64,") for f in frames)

    def test_get_video_info_import_error_returns_minimal(self):
        """get_video_info() cv2 未安装时返回最小信息"""
        from media.video_processor import VideoProcessor

        # 当 cv2 不可用，get_video_info 会捕获异常并返回最小信息
        with patch.dict("sys.modules", {"cv2": None}):
            info = VideoProcessor.get_video_info(b"\x00" * 1024)

        assert info["size_bytes"] == 1024
        assert info["size_mb"] == round(1024 / 1024 / 1024, 2)
        assert "width" not in info

    def test_get_video_info_with_mock_cv2(self):
        """get_video_info() 使用 mock cv2 返回完整信息"""
        from media.video_processor import VideoProcessor

        mock_cv2 = MagicMock()
        mock_cv2.CAP_PROP_FRAME_WIDTH = 3
        mock_cv2.CAP_PROP_FRAME_HEIGHT = 4
        mock_cv2.CAP_PROP_FPS = 5
        mock_cv2.CAP_PROP_FRAME_COUNT = 7

        mock_cap = MagicMock()
        mock_cap.get.side_effect = lambda prop: {
            3: 1920.0,  # width
            4: 1080.0,  # height
            5: 30.0,  # fps
            7: 300.0,  # total_frames
        }.get(prop, 0)
        mock_cap.release = MagicMock()
        mock_cv2.VideoCapture.return_value = mock_cap

        with patch.dict("sys.modules", {"cv2": mock_cv2}):
            info = VideoProcessor.get_video_info(b"\x00" * 2048)

        assert info["width"] == 1920
        assert info["height"] == 1080
        assert info["fps"] == 30.0
        assert info["total_frames"] == 300
        assert "duration_sec" in info

    def test_extract_frames_validation_passes_for_webm(self):
        """extract_frames() WebM 格式通过校验后因 cv2 不可用而抛 ImportError"""
        from media.video_processor import VideoProcessor

        proc = VideoProcessor()
        with patch.dict("sys.modules", {"cv2": None, "numpy": None}):
            with pytest.raises(ImportError):
                proc.extract_frames(b"\x00" * 100, "video/webm")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DocumentProcessor
# ═══════════════════════════════════════════════════════════════════════════════


class TestDocumentProcessor:
    """DocumentProcessor 全面验证"""

    def test_validate_valid_pdf(self):
        """validate() 合法 PDF 数据返回 None"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()
        error = proc.validate(b"\x00" * 100, "application/pdf")
        assert error is None

    def test_validate_unsupported_format(self):
        """validate() 不支持的格式返回错误信息"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()
        error = proc.validate(b"\x00" * 100, "application/json")
        assert "不支持的文档格式" in error

    def test_validate_empty_data(self):
        """validate() 空数据返回错误信息"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()
        error = proc.validate(b"", "application/pdf")
        assert "文档文件为空" in error

    def test_validate_oversized(self):
        """validate() 超大文件返回错误信息"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor(max_size_mb=1)
        big_data = b"\x00" * (2 * 1024 * 1024)
        error = proc.validate(big_data, "application/pdf")
        assert "文档大小超过限制" in error

    def test_validate_all_allowed_types(self):
        """validate() 所有允许的类型都返回 None"""
        from media.document_processor import ALLOWED_DOC_TYPES, DocumentProcessor

        proc = DocumentProcessor()
        for mime in ALLOWED_DOC_TYPES:
            error = proc.validate(b"\x00" * 10, mime)
            assert error is None, f"{mime} should be allowed"

    def test_extract_unsupported_format_raises(self):
        """extract() 不支持的格式抛出 ValueError"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()
        with pytest.raises(ValueError):
            proc.extract(b"\x00" * 100, "application/json")

    def test_extract_empty_data_raises(self):
        """extract() 空数据抛出 ValueError"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()
        with pytest.raises(ValueError, match="文档文件为空"):
            proc.extract(b"", "application/pdf")

    def test_extract_text_plain(self):
        """extract() 纯文本直接解码"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()
        text_data = "这是一个测试文档".encode()
        result = proc.extract(text_data, "text/plain")
        assert result == "这是一个测试文档"

    def test_extract_text_markdown(self):
        """extract() Markdown 文本直接解码"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()
        md_data = "# 标题\n\n内容".encode()
        result = proc.extract(md_data, "text/markdown")
        assert "# 标题" in result

    def test_extract_text_gbk_fallback(self):
        """extract() UTF-8 解码失败时回退到 GBK"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()
        gbk_data = "中文测试".encode("gbk")
        result = proc.extract(gbk_data, "text/plain")
        assert "中文测试" in result

    def test_extract_text_latin1_fallback(self):
        """extract() UTF-8 和 GBK 都失败时回退到 latin-1"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()
        # 制造一个 UTF-8 和 GBK 都无法解码的字节序列
        bad_data = b"\x80\xff\xfe"
        result = proc.extract(bad_data, "text/plain")
        # latin-1 可以解码任何单字节
        assert len(result) > 0

    def test_extract_text_truncation(self):
        """extract() 超长文本被截断至 max_content_length"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor(max_content_length=20)
        long_text = "a" * 100
        result = proc.extract(long_text.encode("utf-8"), "text/plain")
        assert len(result) < 100
        assert "内容截断" in result

    def test_extract_pdf_with_mock(self):
        """extract() 使用 mock pdfplumber 提取 PDF 文本"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()

        # 构建 mock pdfplumber
        mock_page1 = MagicMock()
        mock_page1.extract_text.return_value = "第一页内容"
        mock_page2 = MagicMock()
        mock_page2.extract_text.return_value = "第二页内容"

        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page1, mock_page2]
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)

        mock_pdfplumber = MagicMock()
        mock_pdfplumber.open.return_value = mock_pdf

        with patch.dict("sys.modules", {"pdfplumber": mock_pdfplumber}):
            result = proc.extract(b"%PDF-1.4 fake", "application/pdf")

        assert "第一页内容" in result
        assert "第二页内容" in result

    def test_extract_pdf_import_error(self):
        """extract() pdfplumber 未安装时抛出 ImportError"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()

        with patch.dict("sys.modules", {"pdfplumber": None}):
            with pytest.raises(ImportError, match="pdfplumber 未安装"):
                proc.extract(b"%PDF-1.4 fake", "application/pdf")

    def test_extract_pdf_empty_pages(self):
        """extract() PDF 无可提取文本时返回提示信息"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()

        mock_page = MagicMock()
        mock_page.extract_text.return_value = None

        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page]
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)

        mock_pdfplumber = MagicMock()
        mock_pdfplumber.open.return_value = mock_pdf

        with patch.dict("sys.modules", {"pdfplumber": mock_pdfplumber}):
            result = proc.extract(b"%PDF-1.4 fake", "application/pdf")

        assert "无可提取的文本内容" in result

    def test_extract_docx_with_mock(self):
        """extract() 使用 mock python-docx 提取 Word 文档文本"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()

        mock_para1 = MagicMock()
        mock_para1.text = "段落一"
        mock_para2 = MagicMock()
        mock_para2.text = "段落二"
        mock_para_empty = MagicMock()
        mock_para_empty.text = ""

        mock_doc = MagicMock()
        mock_doc.paragraphs = [mock_para1, mock_para2, mock_para_empty]

        mock_docx_module = MagicMock()
        mock_docx_module.Document.return_value = mock_doc

        with patch.dict("sys.modules", {"docx": mock_docx_module}):
            docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            result = proc.extract(b"PK\x03\x04 fake docx", docx_mime)

        assert "段落一" in result
        assert "段落二" in result
        # 空段落应被过滤
        assert result.count("段落") == 2

    def test_extract_docx_import_error(self):
        """extract() python-docx 未安装时抛出 ImportError"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()

        with patch.dict("sys.modules", {"docx": None}):
            docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            with pytest.raises(ImportError, match="python-docx 未安装"):
                proc.extract(b"PK\x03\x04 fake", docx_mime)

    def test_extract_docx_empty_paragraphs(self):
        """extract() Word 文档无可提取文本时返回提示信息"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()

        mock_para = MagicMock()
        mock_para.text = "   "  # 空白，会被 strip() 过滤

        mock_doc = MagicMock()
        mock_doc.paragraphs = [mock_para]

        mock_docx_module = MagicMock()
        mock_docx_module.Document.return_value = mock_doc

        with patch.dict("sys.modules", {"docx": mock_docx_module}):
            docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            result = proc.extract(b"PK\x03\x04 fake", docx_mime)

        assert "无可提取的文本内容" in result

    def test_extract_pdf_error_handling(self):
        """extract() PDF 解析异常时返回错误提示而非抛出"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()

        mock_pdfplumber = MagicMock()
        mock_pdfplumber.open.side_effect = RuntimeError("corrupt PDF")

        with patch.dict("sys.modules", {"pdfplumber": mock_pdfplumber}):
            result = proc.extract(b"%PDF corrupt", "application/pdf")

        assert "PDF 提取失败" in result

    def test_extract_docx_error_handling(self):
        """extract() DOCX 解析异常时返回错误提示而非抛出"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()

        mock_docx_module = MagicMock()
        mock_docx_module.Document.side_effect = RuntimeError("corrupt docx")

        with patch.dict("sys.modules", {"docx": mock_docx_module}):
            docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            result = proc.extract(b"PK corrupt", docx_mime)

        assert "DOCX 提取失败" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 5. TTSProcessor
# ═══════════════════════════════════════════════════════════════════════════════


class TestTTSProcessor:
    """TTSProcessor 全面验证"""

    @pytest.mark.asyncio
    async def test_synthesize_empty_text_raises(self):
        """synthesize() 空文本抛出 ValueError"""
        from media.tts_processor import TTSProcessor

        proc = TTSProcessor()
        with pytest.raises(ValueError, match="文本不能为空"):
            await proc.synthesize("")

    @pytest.mark.asyncio
    async def test_synthesize_whitespace_text_raises(self):
        """synthesize() 纯空白文本抛出 ValueError"""
        from media.tts_processor import TTSProcessor

        proc = TTSProcessor()
        with pytest.raises(ValueError, match="文本不能为空"):
            await proc.synthesize("   \n\t  ")

    @pytest.mark.asyncio
    async def test_synthesize_import_error(self):
        """synthesize() edge_tts 未安装时抛出 ImportError"""
        from media.tts_processor import TTSProcessor

        proc = TTSProcessor()

        with patch.dict("sys.modules", {"edge_tts": None}):
            with pytest.raises(ImportError, match="edge_tts 未安装"):
                await proc.synthesize("测试文字")

    @pytest.mark.asyncio
    async def test_synthesize_success(self):
        """synthesize() 正常合成返回 MP3 字节"""
        from media.tts_processor import TTSProcessor

        proc = TTSProcessor()

        # mock edge_tts
        mock_edge_tts = MagicMock()

        # 模拟 Communicate.stream() 返回音频 chunk
        async def fake_stream():
            yield {"type": "audio", "data": b"\xff\xfb\x90\x00" * 10}
            yield {"type": "audio", "data": b"\xff\xfb\x90\x00" * 5}
            yield {"type": "WordBoundary", "data": None}  # 非音频 chunk 应被忽略

        mock_communicate = MagicMock()
        mock_communicate.stream = fake_stream
        mock_edge_tts.Communicate.return_value = mock_communicate

        with patch.dict("sys.modules", {"edge_tts": mock_edge_tts}):
            result = await proc.synthesize("你好世界")

        assert isinstance(result, bytes)
        assert len(result) > 0
        mock_edge_tts.Communicate.assert_called_once_with(
            "你好世界", "zh-CN-XiaoxiaoNeural", rate="+0%", volume="+0%"
        )

    @pytest.mark.asyncio
    async def test_synthesize_custom_voice(self):
        """synthesize() 自定义 voice 参数"""
        from media.tts_processor import TTSProcessor

        proc = TTSProcessor()

        mock_edge_tts = MagicMock()

        async def fake_stream():
            yield {"type": "audio", "data": b"\x00" * 50}

        mock_communicate = MagicMock()
        mock_communicate.stream = fake_stream
        mock_edge_tts.Communicate.return_value = mock_communicate

        with patch.dict("sys.modules", {"edge_tts": mock_edge_tts}):
            await proc.synthesize("测试", voice="zh-CN-YunxiNeural")

        mock_edge_tts.Communicate.assert_called_once_with(
            "测试", "zh-CN-YunxiNeural", rate="+0%", volume="+0%"
        )

    @pytest.mark.asyncio
    async def test_synthesize_long_text_truncation(self):
        """synthesize() 超长文本被截断至 2000 字符"""
        from media.tts_processor import TTSProcessor

        proc = TTSProcessor()

        mock_edge_tts = MagicMock()
        truncated_text_ref = []

        async def fake_stream():
            yield {"type": "audio", "data": b"\x00"}

        mock_communicate = MagicMock()
        mock_communicate.stream = fake_stream

        def capture_communicate(text, voice, **kwargs):
            truncated_text_ref.append(text)
            return mock_communicate

        mock_edge_tts.Communicate.side_effect = capture_communicate

        with patch.dict("sys.modules", {"edge_tts": mock_edge_tts}):
            long_text = "a" * 3000
            await proc.synthesize(long_text)

        assert len(truncated_text_ref[0]) < 3000
        assert truncated_text_ref[0].endswith("...")

    def test_list_voices_returns_dict(self):
        """list_voices() 返回语音字典"""
        from media.tts_processor import TTSProcessor

        voices = TTSProcessor.list_voices()
        assert isinstance(voices, dict)
        assert "xiaoxiao" in voices
        assert voices["xiaoxiao"] == "zh-CN-XiaoxiaoNeural"
        # 确保返回的是副本
        voices["test"] = "test"
        assert "test" not in TTSProcessor.list_voices()

    def test_default_voice(self):
        """TTSProcessor 默认语音为 XiaoxiaoNeural"""
        from media.tts_processor import DEFAULT_VOICE, TTSProcessor

        proc = TTSProcessor()
        assert proc.voice == DEFAULT_VOICE
        assert proc.voice == "zh-CN-XiaoxiaoNeural"

    def test_custom_rate_and_volume(self):
        """TTSProcessor 自定义 rate 和 volume 参数"""
        from media.tts_processor import TTSProcessor

        proc = TTSProcessor(rate="+10%", volume="-5%")
        assert proc.rate == "+10%"
        assert proc.volume == "-5%"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. 边界与集成场景
# ═══════════════════════════════════════════════════════════════════════════════


class TestMediaEdgeCases:
    """跨模块边界情况"""

    def test_image_processor_with_webp(self):
        """ImageProcessor 处理 WebP 格式"""
        from media.image_processor import ImageProcessor

        proc = ImageProcessor(max_size_mb=5)
        # 生成 WebP 图片
        from PIL import Image

        img = Image.new("RGB", (50, 50), color=(100, 200, 50))
        buf = io.BytesIO()
        img.save(buf, format="WEBP")
        webp_data = buf.getvalue()

        result = proc.process(webp_data, "image/webp")
        assert result.startswith("data:image/webp;base64,")

    def test_image_get_info_returns_size_mb(self):
        """ImageProcessor.get_info 返回 size_mb 四舍五入"""
        from media.image_processor import ImageProcessor

        data = b"\x00" * (512 * 1024)  # 512KB
        info = ImageProcessor.get_info(data, "image/jpeg")
        assert info["size_mb"] == round(512 * 1024 / 1024 / 1024, 2)

    def test_document_extract_text_with_filename(self):
        """DocumentProcessor.extract() 接受 filename 参数（当前不影响逻辑但不报错）"""
        from media.document_processor import DocumentProcessor

        proc = DocumentProcessor()
        result = proc.extract(b"hello", "text/plain", filename="test.txt")
        assert result == "hello"

    def test_video_processor_custom_params(self):
        """VideoProcessor 自定义参数"""
        from media.video_processor import VideoProcessor

        proc = VideoProcessor(max_size_mb=100, max_frames=20, interval_sec=5.0)
        assert proc.max_size_mb == 100
        assert proc.max_frames == 20
        assert proc.interval_sec == 5.0

    def test_audio_processor_custom_model(self):
        """AudioProcessor 自定义模型名"""
        from media.audio_processor import AudioProcessor

        proc = AudioProcessor(api_key="test-api-key-placeholder", model="whisper-large-v3")
        assert proc.model == "whisper-large-v3"
