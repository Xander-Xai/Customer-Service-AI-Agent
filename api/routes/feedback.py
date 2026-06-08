"""
反馈相关路由：提交反馈、反馈统计
从 api/app.py create_app() 提取。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from logger import get_logger
from api.app import _sanitize_input, FeedbackRequest

router = APIRouter()
logger = get_logger("api.feedback")


@router.post("/api/feedback")
async def submit_feedback(request: Request, data: FeedbackRequest):
    state = request.app.state
    sm = getattr(state, "session_manager", None)
    metrics = getattr(state, "metrics", None)
    bus = getattr(state, "message_bus", None)

    session_id = data.session_id.strip()
    resolved = data.resolved
    rating = data.rating
    message_index = data.message_index
    comment = _sanitize_input(data.comment)

    if not session_id:
        return JSONResponse({"error": "session_id 不能为空"}, status_code=400)

    if sm:
        session = await sm.get_session(session_id)
        if not session or not session.get("messages"):
            return JSONResponse({"error": "会话不存在或无对话记录"}, status_code=404)

    # 持久化反馈到数据库
    try:
        from db.database import get_db_session
        from db.models import Feedback
        db = get_db_session()
        try:
            feedback = Feedback(
                session_id=session_id,
                message_index=message_index,
                rating=rating,
                comment=comment,
                created_at=datetime.now(timezone.utc),
            )
            db.add(feedback)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(f"反馈数据库写入失败: {e}")
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"反馈数据库模块加载失败: {e}")

    if metrics:
        await metrics.record_feedback(resolved=bool(resolved))

    if bus:
        try:
            from core.message_bus import Message, MessageType
            await bus.publish(Message(
                msg_type=MessageType.BROADCAST,
                topic="feedback.received",
                sender="api_feedback",
                payload={"session_id": session_id, "resolved": resolved, "rating": rating, "comment": comment},
            ))
        except Exception as e:
            logger.debug(f"Feedback Bus 事件发布失败: {e}")

    logger.info(f"[Feedback] session={session_id} resolved={resolved} rating={rating} comment={comment[:50]}")
    return {"status": "ok", "session_id": session_id, "resolved": resolved, "rating": rating}


@router.get("/api/feedback/stats")
async def feedback_stats():
    try:
        from db.database import get_db_session
        from db.models import Feedback
        db = get_db_session()
        try:
            total = db.query(Feedback).count()
            positive = db.query(Feedback).filter(Feedback.rating == 1).count()
            negative = db.query(Feedback).filter(Feedback.rating == -1).count()
            rate = round(positive / total, 4) if total > 0 else 0.0
            return {"total": total, "positive": positive, "negative": negative, "rate": rate}
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"反馈统计查询失败: {e}")
        return {"total": 0, "positive": 0, "negative": 0, "rate": 0.0}
