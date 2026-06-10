"""
Prompt 版本管理器（v5.1）
核心能力：
- 从 DB 加载活跃 Prompt 版本（支持热更新）
- 回退到模块级默认 Prompt
- Prompt 效果追踪（关联 Feedback 评分）
- TTL 缓存避免每次请求都查 DB

使用方式：
    manager = PromptManager()
    prompt = manager.get_prompt("product_agent", default_prompt="你是产品专家...")
"""

import threading
import time
from typing import Any

from core.logger import get_logger

logger = get_logger("core.prompt_manager")

# Prompt 缓存 TTL（秒）
CACHE_TTL = 60.0


class PromptManager:
    """Prompt 版本管理器：DB 持久化 + 内存缓存 + 回退机制。

    设计要点：
    1. DB 优先：如果 PromptVersion 表中有 is_active=1 的记录，使用该版本
    2. 回退机制：DB 无记录时使用 agent 文件中的 _SYSTEM_PROMPT 常量
    3. 热更新：缓存 TTL 过期后自动重新加载
    4. 线程安全：使用 threading.Lock 保护缓存
    """

    def __init__(self, cache_ttl: float = CACHE_TTL):
        self._cache_ttl = cache_ttl
        # {agent_name: {"prompt": str, "version": str, "loaded_at": float}}
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._db_available = True
        logger.info("PromptManager 初始化完成")

    def get_prompt(self, agent_name: str, default_prompt: str = "") -> str:
        """获取指定 Agent 的活跃 Prompt。

        优先级：DB 缓存 → DB 查询 → 默认 Prompt

        Args:
            agent_name: Agent 名称（如 "product_agent"）
            default_prompt: 回退用的默认 Prompt

        Returns:
            活跃的 Prompt 文本
        """
        # 1. 检查缓存
        cached = self._cache.get(agent_name)
        if cached and (time.time() - cached["loaded_at"]) < self._cache_ttl:
            return str(cached["prompt"])

        # 2. 从 DB 加载
        if self._db_available:
            try:
                prompt_text, version = self._load_from_db(agent_name)
                if prompt_text:
                    with self._lock:
                        self._cache[agent_name] = {
                            "prompt": prompt_text,
                            "version": version,
                            "loaded_at": time.time(),
                        }
                    logger.debug(f"从 DB 加载 Prompt: {agent_name} v{version}")
                    return prompt_text
            except Exception as e:
                logger.warning(f"DB 加载 Prompt 失败 ({agent_name}): {e}")
                self._db_available = False

        # 3. 回退到默认 Prompt
        return default_prompt

    def get_version_info(self, agent_name: str) -> dict[str, Any]:
        """获取当前使用的 Prompt 版本信息"""
        cached = self._cache.get(agent_name)
        if cached:
            return {
                "agent_name": agent_name,
                "version": cached["version"],
                "loaded_at": cached["loaded_at"],
                "source": "database",
            }
        return {
            "agent_name": agent_name,
            "version": "default",
            "source": "fallback",
        }

    def invalidate(self, agent_name: str | None = None) -> None:
        """清除缓存（用于 Prompt 更新后强制重新加载）"""
        with self._lock:
            if agent_name:
                self._cache.pop(agent_name, None)
                logger.info(f"已清除 {agent_name} 的 Prompt 缓存")
            else:
                self._cache.clear()
                logger.info("已清除所有 Prompt 缓存")
        # 重新启用 DB 访问
        self._db_available = True

    def save_prompt(
        self,
        agent_name: str,
        prompt_text: str,
        version: str,
        activate: bool = True,
    ) -> dict[str, Any]:
        """保存新 Prompt 版本到 DB。

        Args:
            agent_name: Agent 名称
            prompt_text: Prompt 文本
            version: 版本号（如 "v1.0", "v2.0"）
            activate: 是否立即激活（停用同 agent 的其他版本）

        Returns:
            保存后的 PromptVersion 信息
        """
        from db.database import get_db_session
        from db.models import PromptVersion

        db = get_db_session()
        try:
            # 如果激活，先停用同 agent 的其他版本
            if activate:
                db.query(PromptVersion).filter(
                    PromptVersion.agent_name == agent_name,
                    PromptVersion.is_active == 1,
                ).update({"is_active": 0})

            pv = PromptVersion(
                agent_name=agent_name,
                version=version,
                prompt_text=prompt_text,
                is_active=1 if activate else 0,
                score_avg=0.0,
                feedback_count=0,
            )
            db.add(pv)
            db.commit()
            db.refresh(pv)

            # 清除缓存，下次请求时重新加载
            self.invalidate(agent_name)

            logger.info(f"保存 Prompt: {agent_name} v{version} (active={activate})")
            return {
                "id": pv.id,
                "agent_name": pv.agent_name,
                "version": pv.version,
                "is_active": pv.is_active,
                "created_at": pv.created_at.isoformat() if pv.created_at else None,
            }
        except Exception as e:
            db.rollback()
            logger.error(f"保存 Prompt 失败: {e}")
            raise
        finally:
            db.close()

    def list_versions(self, agent_name: str) -> list[dict[str, Any]]:
        """列出指定 Agent 的所有 Prompt 版本"""
        from db.database import get_db_session
        from db.models import PromptVersion

        db = get_db_session()
        try:
            versions = (
                db.query(PromptVersion)
                .filter(PromptVersion.agent_name == agent_name)
                .order_by(PromptVersion.created_at.desc())
                .all()
            )
            return [
                {
                    "id": v.id,
                    "version": v.version,
                    "is_active": bool(v.is_active),
                    "score_avg": v.score_avg,
                    "feedback_count": v.feedback_count,
                    "prompt_preview": v.prompt_text[:200] + "..." if len(v.prompt_text) > 200 else v.prompt_text,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                }
                for v in versions
            ]
        finally:
            db.close()

    def list_agents(self) -> list[str]:
        """列出所有有 Prompt 版本的 Agent"""
        from db.database import get_db_session
        from db.models import PromptVersion

        db = get_db_session()
        try:
            agents = db.query(PromptVersion.agent_name).distinct().all()
            return [a[0] for a in agents]
        finally:
            db.close()

    def record_feedback(self, agent_name: str, score: float) -> None:
        """记录反馈评分，更新 PromptVersion 的 score_avg 和 feedback_count。

        Args:
            agent_name: Agent 名称
            score: 评分（1.0 = 好评, -1.0 = 差评, 或其他数值）
        """
        from db.database import get_db_session
        from db.models import PromptVersion

        # 使用当前缓存的版本
        cached = self._cache.get(agent_name)
        if not cached:
            return

        version = cached.get("version", "")
        if not version or version == "default":
            return

        db = get_db_session()
        try:
            pv = (
                db.query(PromptVersion)
                .filter(
                    PromptVersion.agent_name == agent_name,
                    PromptVersion.version == version,
                )
                .first()
            )
            if pv:
                # 增量更新平均分
                current_count: int = int(pv.feedback_count)
                current_avg: float = float(pv.score_avg)
                total_score = current_avg * current_count + score
                pv.feedback_count = current_count + 1  # type: ignore[assignment]
                pv.score_avg = total_score / (current_count + 1)  # type: ignore[assignment]
                db.commit()
                logger.debug(
                    f"更新 Prompt 评分: {agent_name} v{version} "
                    f"→ score={pv.score_avg:.2f} count={pv.feedback_count}"
                )
        except Exception as e:
            db.rollback()
            logger.warning(f"记录 Prompt 反馈失败: {e}")
        finally:
            db.close()

    def _load_from_db(self, agent_name: str) -> tuple[str | None, str]:
        """从 DB 加载活跃 Prompt。返回 (prompt_text, version) 或 (None, "")"""
        from db.database import get_db_session
        from db.models import PromptVersion

        db = get_db_session()
        try:
            pv = (
                db.query(PromptVersion)
                .filter(
                    PromptVersion.agent_name == agent_name,
                    PromptVersion.is_active == 1,
                )
                .first()
            )
            if pv:
                return str(pv.prompt_text), str(pv.version)
            return None, ""
        finally:
            db.close()


# 全局单例（模块级，由 container 或 app_factory 初始化）
_prompt_manager: PromptManager | None = None


def get_prompt_manager() -> PromptManager | None:
    """获取全局 PromptManager 实例"""
    return _prompt_manager


def init_prompt_manager(cache_ttl: float = CACHE_TTL) -> PromptManager:
    """初始化全局 PromptManager"""
    global _prompt_manager
    if _prompt_manager is None:
        _prompt_manager = PromptManager(cache_ttl=cache_ttl)
    return _prompt_manager
