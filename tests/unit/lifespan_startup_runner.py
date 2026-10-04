"""真实 uvicorn 子进程 runner：验证 lifespan 启动失败的可诊断日志契约（issue #50）。

**不在 pytest 收集范围**（文件名不以 ``test_`` 开头），只由
``tests/unit/test_lifespan_startup_failure.py`` 通过子进程调用。

为什么需要子进程：验收项"exit != 0 / 不静默成功"只能在真实 ASGI 服务器
（uvicorn）进程里观测 —— 在测试进程内直接 ``async with lifespan(...)`` 拿不到
uvicorn 的退出码，也拿不到"服务器是否进入了 serving 状态"。

本 runner 走的是**真实** ``api.app_factory`` 模块（真实 ``lifespan``、真实
``ServiceContainer``、真实 logger 装配），只把失败点注入到指定 stage，因此
测到的是线上同一条代码路径，而不是一个另造的替身。

用法::

    python3 lifespan_startup_runner.py <outdir> <initialize|injection>

``<outdir>`` 用于隔离 ``LOG_DIR``（``core/logger.py`` 在 import 期读取该环境
变量），因此调用方能断言"失败日志是否真的落到了轮转文件里"。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _seed_env_from_repo_dotenv() -> None:
    """先用仓库 ``.env`` 兜底配置，再中和 dotenv。

    ``core/config.py`` 在 import 期执行 ``load_dotenv(override=True)``，若不
    中和，本 runner 显式设置的环境变量会被仓库 ``.env`` 覆盖，测到的就不是
    本用例想验证的那份配置。``setdefault`` 保证调用方传入的值优先。
    """
    from dotenv import dotenv_values

    for key, value in dotenv_values(REPO_ROOT / ".env").items():
        if value is not None:
            os.environ.setdefault(key, value)

    import dotenv

    dotenv.load_dotenv = lambda *a, **k: False


def _main() -> int:
    outdir = Path(sys.argv[1])
    stage = sys.argv[2]

    (outdir / "logs").mkdir(parents=True, exist_ok=True)
    # 隔离 DB：不写仓库 data/csai.db，避免测试污染开发库 / 与其它用例抢锁。
    os.environ["DATABASE_URL"] = f"sqlite:///{outdir / 'lifespan_startup.db'}"
    os.environ["DB_PATH"] = str(outdir / "lifespan_startup.db")
    os.environ["LOG_DIR"] = str(outdir / "logs")
    os.environ["LOG_FORMAT"] = "text"
    _seed_env_from_repo_dotenv()

    import uvicorn

    import api.app_factory as factory_mod
    from core.container import ServiceContainer

    if stage == "initialize":
        # 模拟生产真实失败：checkpoint PostgreSQL 不可达 → ConfigurationError
        # （与 core/container.py::_init_checkpointer 的 fail-closed 分支同形）。
        async def _boom_initialize(self) -> None:
            from core.config import ConfigurationError

            try:
                raise ConnectionError(
                    "could not connect to server: connection refused "
                    "(LANGGRAPH_CHECKPOINT_DATABASE_URL)"
                )
            except ConnectionError as root:
                raise ConfigurationError(
                    "生产环境 LangGraph checkpoint 初始化失败（backend=postgres）："
                    "拒绝回退 MemorySaver，请修复数据库连通性/配置后重启"
                ) from root

        ServiceContainer.initialize = _boom_initialize
    elif stage == "injection":
        # 容器本身初始化成功，但把服务注入 app.state / 模块级引用时炸掉
        # （例如容器实例缺少 graph_app 属性）。
        class _HalfBuiltContainer:
            async def initialize(self) -> None:
                return None

            async def close(self) -> None:
                return None

            @property
            def graph_app(self):
                raise AttributeError("graph_app")

        factory_mod._container = _HalfBuiltContainer()
    else:
        raise SystemExit(f"unknown stage: {stage}")

    uvicorn.run(
        factory_mod.app,
        host="127.0.0.1",
        port=0,
        log_level="info",
        lifespan="auto",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
