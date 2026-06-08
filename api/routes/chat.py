"""
聊天相关路由：REST / SSE 流式 / 多模态图片 / 语音 / TTS / 文件上传
从 api/app.py create_app() 提取，通过 request.app.state 访问依赖。
"""
import asyncio
import io
import json
import os
import time

from fastapi import APIRouter, Request, Form, File, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import MAX_QUERY_LENGTH, MULTIMODAL_ENABLED
from logger import get_logger
from api.utils import sanitize_input, validate_session_id, extract_user_id

router = APIRouter()
logger = get_logger("api.chat")


# ── Pydantic 模型 ──

class ChatRequest(BaseModel):
    query: str = Field(..., max_length=2000)
    session_id: str = Field(default="", max_length=36)
    session_token: str = Field(default="", max_length=64)


class ChatStreamRequest(BaseModel):
    """SSE 流式输出请求模型"""
    query: str = Field(..., max_length=2000)
    session_id: str = Field(default="", max_length=36)
    session_token: str = Field(default="", max_length=64)


# ── SSE 工具 ──

def _sse_event(event_data: dict) -> str:
    return f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"


# ── REST 聊天 ──

@router.post("/api/chat")
async def rest_chat(data: ChatRequest, request: Request):
    state = request.app.state
    run_graph = state.run_graph
    session_manager = state.session_manager

    query = sanitize_input(data.query)[:MAX_QUERY_LENGTH]
    sid = validate_session_id(data.session_id)
    if not query:
        return JSONResponse({"error": "query 不能为空"}, status_code=400)
    client_provided_sid = bool(data.session_id)
    if client_provided_sid and session_manager:
        if not session_manager.validate_session_token(sid, data.session_token or ""):
            return JSONResponse({"error": "会话令牌无效"}, status_code=403)
    try:
        user_id = extract_user_id(request)
        if session_manager and user_id:
            session_manager.set_user_id(sid, user_id)
        result = await run_graph(sid, query)
        # 统一字段映射（对齐 WS/SSE 端点）
        mapped = {
            "response": result.get("response", ""),
            "agent": result.get("current_agent", ""),
            "mode": result.get("collaboration_mode", "sequential"),
            "elapsed": result.get("elapsed", 0),
            "cached": result.get("cached", False),
            "agents_used": result.get("agents_used", []),
            "resolution_status": result.get("resolution_status", ""),
            "session_id": sid,
        }
        if session_manager:
            mapped["session_token"] = session_manager.generate_session_token(sid)
        return mapped
    except Exception as e:
        logger.error(f"REST 处理失败: {e}", exc_info=True)
        return JSONResponse({"error": "服务内部错误，请稍后重试"}, status_code=500)


# ── SSE 流式输出 ──

@router.post("/api/chat/stream")
async def stream_chat(data: ChatStreamRequest, request: Request):
    """SSE 真流式输出端点（v4.2）"""
    state = request.app.state
    run_graph = state.run_graph
    session_manager = state.session_manager

    query = sanitize_input(data.query)[:MAX_QUERY_LENGTH]
    sid = validate_session_id(data.session_id)
    if not query:
        return JSONResponse({"error": "query 不能为空"}, status_code=400)

    client_provided_sid = bool(data.session_id)
    if client_provided_sid and session_manager:
        if not session_manager.validate_session_token(sid, data.session_token or ""):
            return JSONResponse({"error": "会话令牌无效"}, status_code=403)

    user_id = extract_user_id(request)
    if session_manager and user_id:
        session_manager.set_user_id(sid, user_id)

    chunk_queue: asyncio.Queue = asyncio.Queue()

    async def stream_callback(event: dict):
        await chunk_queue.put(event)

    graph_task = asyncio.create_task(run_graph(sid, query, stream_callback))

    async def _event_generator():
        start_time = time.time()
        try:
            yield _sse_event({"type": "status", "content": "正在分析您的问题..."})
            yield _sse_event({"type": "progress", "content": "正在识别意图..."})

            if not graph_task.done():
                while True:
                    try:
                        event = await asyncio.wait_for(chunk_queue.get(), timeout=60.0)
                    except asyncio.TimeoutError:
                        if graph_task.done():
                            break
                        continue
                    if event is None:
                        break
                    if isinstance(event, dict) and event.get("type") == "chunk":
                        yield _sse_event(event)

            try:
                result = await graph_task
            except Exception as e:
                logger.error(f"SSE 图执行失败: {e}")
                yield _sse_event({"type": "error", "content": "服务内部错误，请稍后重试"})
                return

            session_token = ""
            if session_manager and not client_provided_sid:
                session_token = session_manager.generate_session_token(sid)

            elapsed = round(time.time() - start_time, 3)
            yield _sse_event({
                "type": "done",
                "content": result.get("response", ""),
                "agent": result.get("current_agent", ""),
                "mode": result.get("collaboration_mode", "sequential"),
                "elapsed": elapsed,
                "cached": result.get("cached", False),
                "agents_used": result.get("agents_used", []),
                "resolution_status": result.get("resolution_status", ""),
                "session_id": sid,
                "session_token": session_token,
            })
        except Exception as e:
            logger.error(f"SSE 流式处理失败: {e}", exc_info=True)
            yield _sse_event({"type": "error", "content": "服务内部错误，请稍后重试"})

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ── 多模态图片对话 ──

@router.post("/api/chat/image")
async def chat_with_image(
    request: Request,
    query: str = Form(""),
    image: UploadFile = File(...),
    session_id: str = Form(""),
    session_token: str = Form(""),
):
    """v5.1 多模态对话：图片 + 文字（走完整 Agent 图流程）"""
    if not MULTIMODAL_ENABLED:
        return JSONResponse({"error": "多模态功能未启用"}, status_code=400)

    state = request.app.state
    run_graph = state.run_graph
    session_manager = state.session_manager

    query = sanitize_input(query)[:MAX_QUERY_LENGTH]
    sid = validate_session_id(session_id)

    client_provided_sid = bool(session_id)
    if client_provided_sid and session_manager:
        if not session_manager.validate_session_token(sid, session_token or ""):
            return JSONResponse({"error": "会话令牌无效"}, status_code=403)

    try:
        image_bytes = await image.read()
    except Exception as e:
        logger.error(f"图片读取失败: {e}")
        return JSONResponse({"error": "图片读取失败，请重新上传"}, status_code=400)
    if len(image_bytes) == 0:
        return JSONResponse({"error": "上传的图片文件为空"}, status_code=400)

    try:
        from media.image_processor import ImageProcessor
        processor = ImageProcessor()
        data_url = processor.process(image_bytes, image.content_type or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"图片处理失败: {e}", exc_info=True)
        return JSONResponse({"error": "图片处理失败，请稍后重试"}, status_code=500)

    multimodal_content = [{"type": "image_url", "image_url": {"url": data_url}}]

    uid = extract_user_id(request)
    if session_manager and uid:
        session_manager.set_user_id(sid, uid)

    start_time = time.time()
    user_text = query if query else "请分析这张图片"
    try:
        result = await run_graph(sid, user_text, multimodal_content=multimodal_content)
    except Exception as e:
        logger.error(f"多模态图执行失败: {e}", exc_info=True)
        return JSONResponse({"error": "服务内部错误，请稍后重试"}, status_code=500)

    session_token_new = ""
    if session_manager and not client_provided_sid:
        session_token_new = session_manager.generate_session_token(sid)

    return {
        "response": result.get("response", "抱歉，无法分析该图片，请稍后重试。"),
        "session_id": sid,
        "session_token": session_token_new,
        "elapsed": result.get("elapsed", 0),
        "agent": result.get("current_agent", ""),
        "mode": result.get("collaboration_mode", "sequential"),
        "agents_used": result.get("agents_used", []),
    }


# ── 多模态 SSE 流式 ──

@router.post("/api/chat/multimodal/stream")
async def stream_multimodal_chat(
    request: Request,
    query: str = Form(""),
    image: UploadFile = File(...),
    session_id: str = Form(""),
    session_token: str = Form(""),
):
    """v5.1: 多模态 SSE 流式端点"""
    if not MULTIMODAL_ENABLED:
        return JSONResponse({"error": "多模态功能未启用"}, status_code=400)

    state = request.app.state
    run_graph = state.run_graph
    session_manager = state.session_manager

    query = sanitize_input(query)[:MAX_QUERY_LENGTH]
    sid = validate_session_id(session_id)

    client_provided_sid = bool(session_id)
    if client_provided_sid and session_manager:
        if not session_manager.validate_session_token(sid, session_token or ""):
            return JSONResponse({"error": "会话令牌无效"}, status_code=403)

    try:
        image_bytes = await image.read()
    except Exception:
        return JSONResponse({"error": "图片读取失败"}, status_code=400)
    if len(image_bytes) == 0:
        return JSONResponse({"error": "图片文件为空"}, status_code=400)

    try:
        from media.image_processor import ImageProcessor
        processor = ImageProcessor()
        data_url = processor.process(image_bytes, image.content_type or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"图片处理失败: {e}", exc_info=True)
        return JSONResponse({"error": "图片处理失败"}, status_code=500)

    multimodal_content = [{"type": "image_url", "image_url": {"url": data_url}}]
    user_text = query if query else "请分析这张图片"

    uid = extract_user_id(request)
    if session_manager and uid:
        session_manager.set_user_id(sid, uid)

    chunk_queue: asyncio.Queue = asyncio.Queue()

    async def stream_callback(event: dict):
        await chunk_queue.put(event)

    graph_task = asyncio.create_task(run_graph(sid, user_text, stream_callback, multimodal_content=multimodal_content))

    async def _event_generator():
        start_time = time.time()
        try:
            yield _sse_event({"type": "status", "content": "正在分析图片..."})
            yield _sse_event({"type": "progress", "content": "正在识别图片内容..."})

            if not graph_task.done():
                while True:
                    try:
                        event = await asyncio.wait_for(chunk_queue.get(), timeout=60.0)
                    except asyncio.TimeoutError:
                        if graph_task.done():
                            break
                        continue
                    if event is None:
                        break
                    if isinstance(event, dict) and event.get("type") == "chunk":
                        yield _sse_event(event)

            try:
                result = await graph_task
            except Exception as e:
                logger.error(f"多模态图执行失败: {e}")
                yield _sse_event({"type": "error", "content": "服务内部错误"})
                return

            session_token_new = ""
            if session_manager and not client_provided_sid:
                session_token_new = session_manager.generate_session_token(sid)

            elapsed = round(time.time() - start_time, 3)
            yield _sse_event({
                "type": "done",
                "content": result.get("response", ""),
                "agent": result.get("current_agent", ""),
                "mode": result.get("collaboration_mode", "sequential"),
                "elapsed": elapsed,
                "cached": result.get("cached", False),
                "agents_used": result.get("agents_used", []),
                "resolution_status": result.get("resolution_status", ""),
                "session_id": sid,
                "session_token": session_token_new,
            })
        except Exception as e:
            logger.error(f"多模态 SSE 流式处理失败: {e}", exc_info=True)
            yield _sse_event({"type": "error", "content": "服务内部错误"})

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ── 语音对话 ──

@router.post("/api/chat/voice")
async def chat_with_voice(
    request: Request,
    audio: UploadFile = File(...),
    session_id: str = Form(""),
    session_token: str = Form(""),
    language: str = Form("zh"),
):
    """v5.1: 语音对话端点 — 上传音频 → STT 转写 → Agent 图处理 → 文本响应"""
    voice_enabled = os.getenv("VOICE_ENABLED", "false").lower() == "true"
    if not voice_enabled:
        return JSONResponse({"error": "语音功能未启用"}, status_code=400)

    state = request.app.state
    run_graph = state.run_graph
    session_manager = state.session_manager

    sid = validate_session_id(session_id)

    client_provided_sid = bool(session_id)
    if client_provided_sid and session_manager:
        if not session_manager.validate_session_token(sid, session_token or ""):
            return JSONResponse({"error": "会话令牌无效"}, status_code=403)

    try:
        audio_bytes = await audio.read()
    except Exception:
        return JSONResponse({"error": "音频读取失败"}, status_code=400)

    try:
        from media.audio_processor import AudioProcessor
        stt = AudioProcessor()
        query_text = await stt.transcribe(audio_bytes, audio.content_type or "", language)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"STT 转写失败: {e}", exc_info=True)
        return JSONResponse({"error": "语音转写失败，请稍后重试"}, status_code=500)

    if not query_text.strip():
        return JSONResponse({"error": "未识别到有效语音内容"}, status_code=400)

    uid = extract_user_id(request)
    if session_manager and uid:
        session_manager.set_user_id(sid, uid)

    try:
        result = await run_graph(sid, query_text)
    except Exception as e:
        logger.error(f"语音图执行失败: {e}", exc_info=True)
        return JSONResponse({"error": "服务内部错误"}, status_code=500)

    session_token_new = ""
    if session_manager and not client_provided_sid:
        session_token_new = session_manager.generate_session_token(sid)

    return {
        "transcription": query_text,
        "response": result.get("response", ""),
        "session_id": sid,
        "session_token": session_token_new,
        "elapsed": result.get("elapsed", 0),
        "agent": result.get("current_agent", ""),
        "mode": result.get("collaboration_mode", ""),
        "agents_used": result.get("agents_used", []),
    }


# ── TTS 文字转语音 ──

@router.post("/api/tts")
async def text_to_speech(
    request: Request,
    text: str = Form(...),
    voice: str = Form(""),
):
    """v5.1: 文字转语音端点"""
    voice_enabled = os.getenv("VOICE_ENABLED", "false").lower() == "true"
    if not voice_enabled:
        return JSONResponse({"error": "语音功能未启用"}, status_code=400)

    text = sanitize_input(text)
    if not text:
        return JSONResponse({"error": "文本不能为空"}, status_code=400)

    try:
        from media.tts_processor import TTSProcessor
        tts = TTSProcessor()
        audio_bytes = await tts.synthesize(text, voice)
    except ImportError:
        return JSONResponse({"error": "TTS 服务不可用（edge_tts 未安装）"}, status_code=503)
    except Exception as e:
        logger.error(f"TTS 合成失败: {e}", exc_info=True)
        return JSONResponse({"error": "语音合成失败"}, status_code=500)

    return StreamingResponse(
        io.BytesIO(audio_bytes),
        media_type="audio/mpeg",
        headers={"Content-Disposition": "inline; filename=tts.mp3"},
    )


@router.get("/api/tts/voices")
async def list_tts_voices():
    """v5.1: 返回可用 TTS 语音列表"""
    from media.tts_processor import TTSProcessor
    return {"voices": TTSProcessor.list_voices()}


# ── 统一文件上传 ──

@router.post("/api/chat/file")
async def chat_with_file(
    request: Request,
    file: UploadFile = File(...),
    query: str = Form(""),
    session_id: str = Form(""),
    session_token: str = Form(""),
):
    """v5.1: 统一文件上传端点 — 自动识别文件类型分发到对应处理器"""
    state = request.app.state
    run_graph = state.run_graph
    session_manager = state.session_manager

    query = sanitize_input(query)[:MAX_QUERY_LENGTH]
    sid = validate_session_id(session_id)

    client_provided_sid = bool(session_id)
    if client_provided_sid and session_manager:
        if not session_manager.validate_session_token(sid, session_token or ""):
            return JSONResponse({"error": "会话令牌无效"}, status_code=403)

    try:
        file_bytes = await file.read()
    except Exception:
        return JSONResponse({"error": "文件读取失败"}, status_code=400)
    if len(file_bytes) == 0:
        return JSONResponse({"error": "文件为空"}, status_code=400)

    content_type = file.content_type or ""
    uid = extract_user_id(request)
    if session_manager and uid:
        session_manager.set_user_id(sid, uid)

    multimodal_content = None
    user_text = query if query else ""

    # 视频处理
    if content_type.startswith("video/"):
        if not MULTIMODAL_ENABLED:
            return JSONResponse({"error": "多模态功能未启用"}, status_code=400)
        try:
            from media.video_processor import VideoProcessor
            vp = VideoProcessor()
            frames = vp.extract_frames(file_bytes, content_type)
            if not frames:
                return JSONResponse({"error": "无法从视频中提取帧"}, status_code=400)
            multimodal_content = [{"type": "image_url", "image_url": {"url": f}} for f in frames]
            if not user_text:
                user_text = f"请分析这段视频的内容（共提取了 {len(frames)} 帧）"
        except ImportError:
            return JSONResponse({"error": "视频处理不可用（opencv 未安装）"}, status_code=503)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    # 文档处理
    elif content_type in ("application/pdf",
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                          "text/plain", "text/markdown"):
        try:
            from media.document_processor import DocumentProcessor
            dp = DocumentProcessor()
            doc_text = dp.extract(file_bytes, content_type, file.filename or "")
            user_text = f"{user_text}\n\n[文档内容]\n{doc_text}" if user_text else f"[文档内容]\n{doc_text}"
            if not user_text:
                user_text = "请总结这个文档的内容"
        except ImportError:
            return JSONResponse({"error": "文档处理不可用（依赖未安装）"}, status_code=503)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    # 图片处理
    elif content_type.startswith("image/"):
        if not MULTIMODAL_ENABLED:
            return JSONResponse({"error": "多模态功能未启用"}, status_code=400)
        try:
            from media.image_processor import ImageProcessor
            processor = ImageProcessor()
            data_url = processor.process(file_bytes, content_type)
            multimodal_content = [{"type": "image_url", "image_url": {"url": data_url}}]
            if not user_text:
                user_text = "请分析这张图片"
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    else:
        return JSONResponse({"error": f"不支持的文件类型: {content_type}"}, status_code=400)

    try:
        result = await run_graph(sid, user_text, multimodal_content=multimodal_content)
    except Exception as e:
        logger.error(f"文件处理图执行失败: {e}", exc_info=True)
        return JSONResponse({"error": "服务内部错误"}, status_code=500)

    session_token_new = ""
    if session_manager and not client_provided_sid:
        session_token_new = session_manager.generate_session_token(sid)

    return {
        "response": result.get("response", ""),
        "session_id": sid,
        "session_token": session_token_new,
        "elapsed": result.get("elapsed", 0),
        "agent": result.get("current_agent", ""),
        "mode": result.get("collaboration_mode", ""),
        "agents_used": result.get("agents_used", []),
    }
