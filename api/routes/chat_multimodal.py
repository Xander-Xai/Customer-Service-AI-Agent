"""
多模态聊天路由：图片分析 / 文件上传 / 语音识别 / TTS
从 api/routes/chat.py 拆分，共享认证和会话验证逻辑。
"""

import time

from fastapi import APIRouter, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse

from api.utils import sanitize_input
from core.concurrency.distributed_lock import ThreadBusyError
from core.config import MAX_QUERY_LENGTH, MULTIMODAL_ENABLED
from core.logger import get_logger

from .chat import _SessionValidationError, get_authenticated_session

router = APIRouter()
logger = get_logger("api.chat.multimodal")

# 文件上传大小限制（与 middleware.py 的 5MB POST body 限制一致）
_MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5MB


# ===== 图片上传处理 =====


async def _handle_image_upload(
    image: UploadFile,
    query: str,
    request: Request,
):
    """处理图片上传 + 文字查询（v6.1: 使用 ImageProcessor 替代内联 base64）"""
    if not MULTIMODAL_ENABLED:
        return JSONResponse({"error": "图片识别功能未启用"}, status_code=503)

    content_type = image.content_type or ""
    # 限定安全的图片 MIME 类型（拒绝 image/svg+xml 等含 XSS 风险的格式）
    _SAFE_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
    if content_type not in _SAFE_IMAGE_TYPES:
        return JSONResponse({"error": "仅支持 JPEG/PNG/WebP 图片格式"}, status_code=400)

    from core.config import MAX_IMAGE_SIZE_MB

    max_bytes = MAX_IMAGE_SIZE_MB * 1024 * 1024
    image_data = await image.read()
    if len(image_data) > max_bytes:
        return JSONResponse(
            {"error": f"图片大小超过限制（最大 {MAX_IMAGE_SIZE_MB}MB）"},
            status_code=400,
        )

    # v6.1: 使用 ImageProcessor 处理图片（压缩/验证/data URL）
    try:
        from media.image_processor import ImageProcessor

        processor = ImageProcessor()
        data_url = processor.process(image_data, content_type)
    except Exception:
        # 降级：回退到内联 base64
        import base64

        base64_image = base64.b64encode(image_data).decode("utf-8")
        data_url = f"data:{content_type};base64,{base64_image}"

    multimodal_content = [
        {"type": "text", "text": query or "请分析这张图片"},
        {
            "type": "image_url",
            "image_url": {"url": data_url},
        },
    ]

    return multimodal_content


@router.post("/api/chat/image")
async def chat_with_image(
    request: Request,
    image: UploadFile = File(...),
    query: str = Form(default=""),
    session_id: str = Form(default=""),
    session_token: str = Form(default=""),
):
    """多模态图片分析接口"""
    try:
        auth = await get_authenticated_session(request, session_id, session_token)
    except _SessionValidationError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)

    multimodal_content = await _handle_image_upload(image, query, request)
    if isinstance(multimodal_content, JSONResponse):
        return multimodal_content

    query = sanitize_input(query[:MAX_QUERY_LENGTH]) or "请分析这张图片"
    run_graph = request.app.state.run_graph
    start = time.time()
    result = await run_graph(
        auth.sid, query, multimodal_content=multimodal_content, user_id=auth.user_id
    )
    elapsed = time.time() - start

    return {
        "type": "image",
        "response": result.get("response", ""),
        "agent": result.get("current_agent", ""),
        "mode": result.get("collaboration_mode", "sequential"),
        "elapsed": elapsed,
        "session_id": auth.sid,
        "session_token": auth.session_manager.generate_session_token(auth.sid)
        if auth.session_manager
        else "",
        "agents_used": result.get("agents_used", []),
    }


@router.post("/api/chat/multimodal/stream")
async def stream_multimodal_chat(
    request: Request,
    image: UploadFile = File(...),
    query: str = Form(default=""),
    session_id: str = Form(default=""),
    session_token: str = Form(default=""),
):
    """多模态图片分析 SSE 流式接口"""
    try:
        auth = await get_authenticated_session(request, session_id, session_token)
    except _SessionValidationError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)

    multimodal_content = await _handle_image_upload(image, query, request)
    if isinstance(multimodal_content, JSONResponse):
        return multimodal_content

    query = sanitize_input(query[:MAX_QUERY_LENGTH]) or "请分析这张图片"

    from .chat import SSEStreamContext, _build_sse_stream_context, _sse_stream_generator

    graph_task, chunk_queue = _build_sse_stream_context(
        request,
        auth.sid,
        query,
        auth.session_manager,
        auth.client_provided_sid,
        multimodal_content,
        user_id=auth.user_id,
    )

    ctx = SSEStreamContext(
        graph_task=graph_task,
        chunk_queue=chunk_queue,
        sid=auth.sid,
        session_manager=auth.session_manager,
        client_provided_sid=auth.client_provided_sid,
        status_msg="正在分析图片...",
        progress_msg="正在识别图片内容...",
    )

    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        _sse_stream_generator(ctx),
        media_type="text/event-stream",
    )


# ===== 语音识别 =====


async def _handle_audio_upload(audio: UploadFile, request: Request):
    """处理音频上传，返回识别文本"""
    from media.audio_processor import AudioProcessor

    # 前置大小检查（避免内存溢出）
    cl = request.headers.get("content-length")
    if cl and int(cl) > _MAX_UPLOAD_SIZE:
        return JSONResponse({"error": "音频文件过大"}, status_code=413)
    audio_data = await audio.read()
    if len(audio_data) > _MAX_UPLOAD_SIZE:
        return JSONResponse({"error": "音频文件大小超过限制（最大 5MB）"}, status_code=413)
    processor = AudioProcessor()
    text = await processor.transcribe(audio_data, audio.content_type or "")
    return text


@router.post("/api/chat/voice")
async def chat_with_voice(
    request: Request,
    audio: UploadFile = File(...),
    session_id: str = Form(default=""),
    session_token: str = Form(default=""),
):
    """语音识别 + 文字对话"""
    try:
        auth = await get_authenticated_session(request, session_id, session_token)
    except _SessionValidationError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)

    try:
        text = await _handle_audio_upload(audio, request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except ImportError as e:
        logger.warning(f"语音识别依赖缺失: {e}")
        return JSONResponse({"error": "语音识别服务未就绪"}, status_code=503)
    except Exception as e:
        logger.error(f"语音识别失败: {e}", exc_info=True)
        return JSONResponse({"error": "语音识别失败，请稍后重试"}, status_code=500)

    if isinstance(text, JSONResponse):
        return text
    if not text:
        return JSONResponse({"error": "语音识别失败，请重试"}, status_code=400)

    run_graph = request.app.state.run_graph
    start = time.time()
    result = await run_graph(auth.sid, text)
    elapsed = time.time() - start

    return {
        "type": "voice",
        "response": result.get("response", ""),
        "agent": result.get("current_agent", ""),
        "elapsed": elapsed,
        "session_id": auth.sid,
        "session_token": auth.session_manager.generate_session_token(auth.sid)
        if auth.session_manager
        else "",
        "transcription": text,
    }


# ===== TTS 文字转语音 =====


@router.post("/api/tts")
async def text_to_speech(request: Request):
    """文字转语音（支持 JSON 和 FormData 两种格式，可选 voice 参数）"""
    from fastapi.responses import Response

    content_type = request.headers.get("content-type", "")
    voice = ""
    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        text = str(form.get("text", "")).strip()
        voice = str(form.get("voice", "")).strip()
    else:
        body = await request.json()
        text = body.get("text", "").strip()
        voice = body.get("voice", "").strip()
    if not text:
        return JSONResponse({"error": "文本不能为空"}, status_code=400)

    from media.tts_processor import TTSProcessor

    processor = TTSProcessor()
    audio_data = await processor.synthesize(text, voice=voice)
    if not audio_data:
        return JSONResponse({"error": "语音合成失败"}, status_code=500)

    return Response(content=audio_data, media_type="audio/mpeg")


@router.get("/api/tts/voices")
async def list_tts_voices():
    """列出可用的 TTS 语音"""
    from media.tts_processor import TTSProcessor

    processor = TTSProcessor()
    return {"voices": processor.list_voices()}


# ===== 通用文件上传 =====


async def _handle_document_upload(file: UploadFile, query: str, request: Request):
    """处理文档上传（PDF/DOCX/TXT/MD）"""
    from media.document_processor import DocumentProcessor

    # 大小检查
    cl = request.headers.get("content-length")
    if cl and int(cl) > _MAX_UPLOAD_SIZE:
        return None
    file_data = await file.read()
    if len(file_data) > _MAX_UPLOAD_SIZE:
        return None
    content_type = file.content_type or ""
    processor = DocumentProcessor()
    text = processor.extract(file_data, content_type, file.filename or "")
    return text


async def _handle_video_upload(file: UploadFile, query: str, request: Request):
    """处理视频上传"""
    from media.video_processor import VideoProcessor

    # 大小检查
    cl = request.headers.get("content-length")
    if cl and int(cl) > _MAX_UPLOAD_SIZE:
        return None
    file_data = await file.read()
    if len(file_data) > _MAX_UPLOAD_SIZE:
        return None
    content_type = file.content_type or ""
    processor = VideoProcessor()
    frames = processor.extract_frames(file_data, content_type)
    if not frames:
        return "无法从视频中提取有效帧"
    return f"视频分析完成，提取了 {len(frames)} 个关键帧"


@router.post("/api/chat/file")
async def chat_with_file(
    request: Request,
    file: UploadFile = File(...),
    query: str = Form(default=""),
    session_id: str = Form(default=""),
    session_token: str = Form(default=""),
):
    """统一文件上传对话接口（图片/视频/PDF/DOCX/文本）"""
    try:
        auth = await get_authenticated_session(request, session_id, session_token)
    except _SessionValidationError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)

    content_type = file.content_type or ""
    filename = file.filename or ""

    # 根据文件类型分流处理
    if content_type.startswith("image/"):
        multimodal_content = await _handle_image_upload(file, query, request)
        if isinstance(multimodal_content, JSONResponse):
            return multimodal_content
        query = sanitize_input(query[:MAX_QUERY_LENGTH]) or "请分析这张图片"
        run_graph = request.app.state.run_graph
        start = time.time()
        result = await run_graph(auth.sid, query, multimodal_content=multimodal_content)
    elif content_type.startswith("video/"):
        video_desc = await _handle_video_upload(file, query, request)
        if video_desc is None:
            return JSONResponse({"error": "视频文件过大"}, status_code=413)
        combined_query = (
            f"{query}\n\n视频分析结果：{video_desc}" if query else f"视频分析结果：{video_desc}"
        )
        combined_query = sanitize_input(combined_query[:MAX_QUERY_LENGTH])
        run_graph = request.app.state.run_graph
        start = time.time()
        result = await run_graph(auth.sid, combined_query)
    elif any(filename.lower().endswith(ext) for ext in (".pdf", ".docx", ".doc", ".txt", ".md")):
        doc_text = await _handle_document_upload(file, query, request)
        if doc_text is None:
            return JSONResponse({"error": "文档文件过大"}, status_code=413)
        combined_query = (
            f"{query}\n\n文档内容：{doc_text}" if query else f"请分析以下文档内容：\n{doc_text}"
        )
        combined_query = sanitize_input(combined_query[:MAX_QUERY_LENGTH])
        run_graph = request.app.state.run_graph
        start = time.time()
        result = await run_graph(auth.sid, combined_query)
    else:
        return JSONResponse(
            {"error": "不支持的文件格式。支持: 图片、视频、PDF、DOCX、TXT/MD"},
            status_code=400,
        )

    elapsed = time.time() - start

    return {
        "response": result.get("response", ""),
        "agent": result.get("current_agent", ""),
        "mode": result.get("collaboration_mode", "sequential"),
        "elapsed": elapsed,
        "session_id": auth.sid,
        "session_token": auth.session_manager.generate_session_token(auth.sid)
        if auth.session_manager
        else "",
        "agents_used": result.get("agents_used", []),
    }


@router.post("/api/chat/multimodal")
async def chat_multimodal(
    request: Request,
    file: UploadFile = File(None),
    message: str = Form(""),
    file_type: str = Header("auto"),
    session_id: str = Form(default=""),
    session_token: str = Form(default=""),
):
    """统一多模态入口：自动检测文件类型并路由到对应处理器。

    支持 voice/image/document 文件预处理，返回提取内容。
    认证与会话：与 /api/chat/voice 等端点使用相同认证模式。
    """
    # 会话认证
    try:
        auth = await get_authenticated_session(request, session_id, session_token)
    except _SessionValidationError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    # If no file, return informative message
    if not file:
        return {"type": "text", "message": message, "note": "纯文本消息请走 /api/chat 端点"}

    # Determine file type from header or auto-detect from content_type
    ftype = file_type
    if ftype == "auto":
        ct = file.content_type or ""
        if ct.startswith("audio/"):
            ftype = "voice"
        elif ct.startswith("image/"):
            ftype = "image"
        else:
            ftype = "unknown"

    content = await file.read()

    # 文件大小校验
    if len(content) > _MAX_UPLOAD_SIZE:
        return JSONResponse({"error": "文件大小超过限制（最大 5MB）"}, status_code=413)

    if ftype == "voice":
        try:
            from media.audio_processor import AudioProcessor

            processor = AudioProcessor()
            transcription = await processor.transcribe(content, file.content_type or "")
            if not transcription:
                return JSONResponse({"error": "语音转录结果为空"}, status_code=400)

            # v6.1: 调用 run_graph 实现完整 AI 对话
            run_graph = request.app.state.run_graph
            start = time.time()
            result = await run_graph(auth.sid, transcription)
            elapsed = time.time() - start

            return {
                "type": "voice",
                "response": result.get("response", ""),
                "agent": result.get("current_agent", ""),
                "mode": result.get("collaboration_mode", "sequential"),
                "elapsed": elapsed,
                "session_id": auth.sid,
                "session_token": auth.session_manager.generate_session_token(auth.sid)
                if auth.session_manager
                else "",
                "transcription": transcription,
                "message": transcription,
            }
        except ThreadBusyError:
            raise  # 交给全局 handler -> 409
        except Exception as e:
            logger.warning(f"语音处理失败: {e}")
            return JSONResponse({"error": "语音转录处理失败"}, status_code=500)

    elif ftype == "image":
        try:
            from media.image_processor import ImageProcessor

            processor = ImageProcessor()
            data_url = processor.process(content, file.content_type or "")

            # 构建 multimodal_content 供 graph 使用
            multimodal_content = [
                {"type": "text", "text": message or "请分析这张图片"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]

            # v6.1: 调用 run_graph 实现完整 AI 对话
            query = sanitize_input((message or "请分析这张图片")[:MAX_QUERY_LENGTH])
            run_graph = request.app.state.run_graph
            start = time.time()
            result = await run_graph(auth.sid, query, multimodal_content=multimodal_content)
            elapsed = time.time() - start

            return {
                "type": "image",
                "response": result.get("response", ""),
                "agent": result.get("current_agent", ""),
                "mode": result.get("collaboration_mode", "sequential"),
                "elapsed": elapsed,
                "session_id": auth.sid,
                "session_token": auth.session_manager.generate_session_token(auth.sid)
                if auth.session_manager
                else "",
                "image_url": data_url,
                "message": message,
            }
        except ThreadBusyError:
            raise  # 交给全局 handler -> 409
        except Exception as e:
            logger.warning(f"图片处理失败: {e}")
            return JSONResponse({"error": "图片分析处理失败"}, status_code=500)

    elif ftype == "document":
        try:
            from media.document_processor import DocumentProcessor

            processor = DocumentProcessor()
            text = processor.extract(content, file.content_type or "", file.filename or "")
            return {
                "type": "document",
                "content": text,
                "message": message,
            }
        except Exception as e:
            logger.warning(f"文档处理失败: {e}")
            return JSONResponse({"error": "文档内容提取失败"}, status_code=500)

    else:
        return JSONResponse(
            {"error": f"不支持的文件类型: {file.content_type}"},
            status_code=400,
        )
