"""
VideoProcessor — 视频抽帧处理（v5.1）

功能:
- 视频格式校验（MP4/WebM/MOV）
- 关键帧提取（按间隔抽帧，最多 MAX_FRAMES 帧）
- 帧转 base64 data URL
- 大小限制
"""

import base64

from core.logger import get_logger

logger = get_logger("media.video")

ALLOWED_VIDEO_TYPES = ["video/mp4", "video/webm", "video/quicktime"]
MAX_VIDEO_SIZE_MB = 50
MAX_FRAMES = 10  # 最多提取帧数
FRAME_INTERVAL_SEC = 2  # 每 N 秒提取一帧


class VideoProcessor:
    """视频处理器：抽帧 → 转为图片列表"""

    def __init__(
        self,
        max_size_mb: int = MAX_VIDEO_SIZE_MB,
        max_frames: int = MAX_FRAMES,
        interval_sec: float = FRAME_INTERVAL_SEC,
    ):
        self.max_size_mb = max_size_mb
        self.max_frames = max_frames
        self.interval_sec = interval_sec

    def validate(self, data: bytes, content_type: str) -> str | None:
        """校验视频文件，返回错误信息或 None"""
        if content_type not in ALLOWED_VIDEO_TYPES:
            return f"不支持的视频格式: {content_type}，支持: MP4/WebM/MOV"
        if len(data) > self.max_size_mb * 1024 * 1024:
            return f"视频大小超过限制（最大 {self.max_size_mb}MB）"
        if len(data) == 0:
            return "视频文件为空"
        return None

    def extract_frames(self, video_bytes: bytes, content_type: str) -> list[str]:
        """
        从视频中提取关键帧，返回 base64 data URL 列表

        Args:
            video_bytes: 视频字节数据
            content_type: MIME 类型

        Returns:
            base64 data URL 列表（每帧一个）

        Raises:
            ValueError: 格式或大小不合法
            ImportError: opencv 未安装
        """
        error = self.validate(video_bytes, content_type)
        if error:
            raise ValueError(error)

        try:
            import cv2
            import numpy as np  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "opencv-python-headless 未安装，请运行: pip install opencv-python-headless"
            ) from e

        # 写入临时文件（OpenCV 需要文件路径）
        import tempfile

        ext_map = {"video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov"}
        ext = ext_map.get(content_type, ".mp4")

        with tempfile.NamedTemporaryFile(suffix=ext, delete=True) as tmp:
            tmp.write(video_bytes)
            tmp.flush()

            cap = cv2.VideoCapture(tmp.name)
            if not cap.isOpened():
                raise ValueError("无法打开视频文件")

            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            interval_frames = int(fps * self.interval_sec)

            frames = []
            frame_idx = 0

            while cap.isOpened() and len(frames) < self.max_frames:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ret, frame = cap.read()
                if not ret:
                    break

                # 缩放（长边不超过 1024）
                h, w = frame.shape[:2]
                if max(h, w) > 1024:
                    ratio = 1024 / max(h, w)
                    frame = cv2.resize(frame, (int(w * ratio), int(h * ratio)))

                # 转为 JPEG base64
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                b64 = base64.b64encode(buf).decode("utf-8")
                frames.append(f"data:image/jpeg;base64,{b64}")

                frame_idx += interval_frames

            cap.release()
            logger.info(f"视频抽帧完成: {total_frames} 帧 → {len(frames)} 帧")
            return frames

    @staticmethod
    def get_video_info(video_bytes: bytes) -> dict:
        """获取视频基本信息"""
        try:
            import tempfile

            import cv2

            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=True) as tmp:
                tmp.write(video_bytes)
                tmp.flush()
                cap = cv2.VideoCapture(tmp.name)
                info = {
                    "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    "fps": cap.get(cv2.CAP_PROP_FPS),
                    "total_frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
                    "duration_sec": round(
                        int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) / (cap.get(cv2.CAP_PROP_FPS) or 25),
                        1,
                    ),
                    "size_bytes": len(video_bytes),
                    "size_mb": round(len(video_bytes) / 1024 / 1024, 2),
                }
                cap.release()
                return info
        except Exception:
            return {
                "size_bytes": len(video_bytes),
                "size_mb": round(len(video_bytes) / 1024 / 1024, 2),
            }
