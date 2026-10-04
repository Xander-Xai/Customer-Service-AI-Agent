"""lifespan / ServiceContainer 启动失败的"可诊断 + fail closed"契约（issue #50）。

背景（本文件钉死的是行为契约，不是覆盖率）：

进程启动失败时，取证窗口**只有这一条日志** —— 进程随即退出，内存里的 traceback
一去不返。修复前 ``api/app_factory.py::lifespan`` 对 ``_container.initialize()``
既不记日志也不加标注，唯一痕迹是 uvicorn 自己那句无定位信息的
``Exception in 'lifespan' protocol``（或 ``ERROR: Traceback``）：既不落进项目
轮转日志 ``logs/app.log``，也不带"失败在哪一步 / 该怎么办"。

契约四要素（缺一不可）：

1. **定位**：日志带 ``event`` / ``phase`` / ``stage``，指明生命周期哪一步失败；
2. **类型**：失败异常与**根因**异常各自的类名与消息（沿 ``__cause__`` 链下钻）；
3. **证据**：完整 traceback（含异常链）；
4. **动作**：可据以行动的处置建议。

同时必须**不吞异常**：记录之后原样 ``raise``，由 ASGI 服务器以非零码退出 ——
记录日志绝不能变成"带病启动"的借口。

覆盖面：

- ``TestLifespanStartupFailureContract``：进程内驱动真实 ``lifespan``，钉死
  "记录 + 重新抛出"（旧代码上**没有任何 CRITICAL 记录**，因此失败）。
- ``TestLogStartupFailure``：``core.logger.log_startup_failure`` 自身的契约
  （JSON 生产面、异常链环安全性、脱离 ``except`` 块仍能取到 traceback）。
- ``TestLifespanStartupFailureUnderUvicorn``：真实 uvicorn 子进程，钉死验收项
  ``exit != 0`` / 根因在日志里 / traceback 在日志里 / 未静默成功。
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = Path(__file__).resolve().parent / "lifespan_startup_runner.py"

#: 成功启动才会出现的日志；启动失败时必须不存在（否则就是"静默成功"）。
_SUCCESS_LOG_MARKER = "ServiceContainer 初始化完成，所有服务就绪"
#: uvicorn 只有在 lifespan startup 成功之后才会打印这一行。
_UVICORN_STARTUP_COMPLETE = "Application startup complete."

_EVENT_MARKER = "event=lifespan_startup_failed"


@contextlib.contextmanager
def _capture_app_factory_logs(level: int = logging.DEBUG):
    """把捕获 handler 直接挂到 ``app_factory`` logger 上收集记录。

    ``core.logger.get_logger`` 统一 ``propagate=False``，因此 pytest 的
    ``caplog``（挂在 root handler 上）看不到这些记录 —— 与
    ``test_erp_authorization.py`` / ``test_mcp_adapter.py`` 同因同解。
    """
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler()
    handler.setLevel(level)
    target = logging.getLogger("app_factory")
    target.addHandler(handler)
    try:
        yield records
    finally:
        target.removeHandler(handler)


def _isolated_logger(name: str) -> tuple[logging.Logger, list[logging.LogRecord]]:
    """构造一个不注册进 ``logging`` manager 的独立 logger + 记录捕获列表。

    直接 ``logging.Logger(name)`` 而非 ``getLogger(name)``：不污染全局
    logger 注册表，也不需要事后清理 handler。
    """
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.Logger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(_Handler())
    return logger, records


def _configuration_error() -> type[Exception]:
    """运行时解析 ``core.config.ConfigurationError``。

    刻意不在模块作用域 import：``tests/unit/test_hitl_api.py`` 会对
    ``core.config`` 做 ``importlib.reload``，reload 会重建模块里的类对象。
    （与 ``tests/unit/test_mcp_init_rollback.py`` 同一处理。）
    """
    import core.config

    return core.config.ConfigurationError


def _startup_failure_exc() -> BaseException:
    """构造与生产同形的启动失败：``ConfigurationError`` ← ``ConnectionError``。

    在真实 ``except`` 块里 raise 再返回，才能拿到真正的 ``__cause__`` 链与
    traceback，而不是手工伪造 ``__cause__`` 属性。
    """
    configuration_error = _configuration_error()
    try:
        raise ConnectionError(
            "could not connect to server: connection refused (LANGGRAPH_CHECKPOINT_DATABASE_URL)"
        )
    except ConnectionError as root:
        try:
            raise configuration_error(
                "生产环境 LangGraph checkpoint 初始化失败（backend=postgres）："
                "拒绝回退 MemorySaver，请修复数据库连通性/配置后重启"
            ) from root
        except configuration_error as chained:
            return chained
    raise AssertionError("unreachable")


def _mock_app() -> SimpleNamespace:
    """最小 app 替身：只需 ``app.state`` 可写属性赋值。"""
    return SimpleNamespace(state=SimpleNamespace())


class _ExplodingContainer:
    """``initialize()`` 以给定异常失败的容器替身。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.initialize_calls = 0
        self.close_calls = 0

    async def initialize(self) -> None:
        self.initialize_calls += 1
        raise self._exc

    async def close(self) -> None:
        self.close_calls += 1


class _FullContainer:
    """初始化成功、句柄齐全的容器替身；可让 ``graph_app`` 取值即抛错。"""

    def __init__(self, *, explode_on_graph_app: bool = False) -> None:
        self._explode_on_graph_app = explode_on_graph_app
        self.initialize_calls = 0
        self.close_calls = 0

    async def initialize(self) -> None:
        self.initialize_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    def __getattr__(self, name: str) -> object:
        # 只在显式要求时炸；其余服务句柄一律给占位对象（注入阶段会全部写入）。
        if name == "graph_app" and self._explode_on_graph_app:
            raise AttributeError("graph_app")
        return object()


@pytest.mark.unit
class TestLifespanStartupFailureContract:
    """进程内：``lifespan`` 必须"记录 + 重新抛出"，且记录内容完整。"""

    @pytest.mark.asyncio
    async def test_initialize_failure_is_logged_with_traceback_and_rethrown(self):
        """container.initialize() 失败：落 CRITICAL 日志后异常继续向上抛。"""
        from unittest.mock import patch

        import api.app_factory as factory_mod

        exc = _startup_failure_exc()
        container = _ExplodingContainer(exc)

        with (
            _capture_app_factory_logs() as records,
            patch.object(factory_mod, "_container", container),
            pytest.raises(type(exc)) as raised,
        ):
            async with factory_mod.lifespan(_mock_app()):
                pytest.fail("lifespan 必须在初始化失败时抛出，绝不 yield")

        # 1) fail closed：异常原样传播，未被吞掉、未被替换。
        assert raised.value is exc, "lifespan 必须原样重新抛出同一个异常对象"

        criticals = [r for r in records if r.levelno >= logging.CRITICAL]
        assert criticals, (
            "lifespan 初始化失败必须留下 CRITICAL 日志；"
            f"实际记录级别={[r.levelname for r in records]}"
        )
        record = criticals[-1]
        message = record.getMessage()

        # 2) 定位：失败在生命周期的哪一步。
        assert _EVENT_MARKER in message
        assert "stage=container.initialize" in message

        # 3) 类型：失败异常 + 根因异常的类名与消息。
        assert "exc=ConfigurationError" in message
        assert "拒绝回退 MemorySaver" in message
        assert "根因=ConnectionError" in message
        assert "connection refused" in message

        # 4) 动作：可据以行动的处置建议（非空、含具体配置项与复核命令）。
        assert "处置建议：" in message
        remediation = message.split("处置建议：", 1)[1].strip()
        assert remediation, "处置建议不得为空"
        assert "LANGGRAPH_CHECKPOINT_BACKEND" in remediation
        assert "make runtime-verify" in remediation

        # 5) 证据：完整 traceback，且异常链两级都在。
        assert record.exc_info, "CRITICAL 记录必须携带 exc_info（完整 traceback）"
        traceback_text = logging.Formatter().formatException(record.exc_info)
        assert "Traceback (most recent call last)" in traceback_text
        assert "The above exception was the direct cause of" in traceback_text, (
            "traceback 必须保留 __cause__ 链（否则只剩包装后的类型，根因丢失）"
        )
        assert "ConnectionError: could not connect to server" in traceback_text
        assert "ConfigurationError: 生产环境 LangGraph checkpoint 初始化失败" in traceback_text
        # traceback 必须锚定在 lifespan 的真实失败调用点上（证明"失败在哪一步"）。
        assert "in lifespan" in traceback_text
        assert "await _container.initialize()" in traceback_text

        # 6) 结构化字段（生产 JSON 日志面）：供日志系统按字段聚合告警。
        assert getattr(record, "event", None) == "lifespan_startup_failed"
        assert getattr(record, "phase", None) == "startup"
        assert getattr(record, "stage", None) == "container.initialize"
        assert getattr(record, "exception_type", None) == "ConfigurationError"
        assert getattr(record, "root_exception_type", None) == "ConnectionError"

    @pytest.mark.asyncio
    async def test_injection_failure_is_logged_with_its_own_stage_and_rethrown(self):
        """app.state / 模块级注入失败：同样记录，且 stage 指向注入阶段。"""
        from unittest.mock import patch

        import api.app_factory as factory_mod

        container = _FullContainer(explode_on_graph_app=True)

        with (
            _capture_app_factory_logs() as records,
            patch.object(factory_mod, "_container", container),
            pytest.raises(AttributeError),
        ):
            async with factory_mod.lifespan(_mock_app()):
                pytest.fail("注入阶段失败时 lifespan 绝不能 yield")

        assert container.initialize_calls == 1, "注入阶段之前必须已成功初始化容器"
        criticals = [r for r in records if r.levelno >= logging.CRITICAL]
        assert criticals, "注入阶段失败也必须留下 CRITICAL 日志"
        record = criticals[-1]
        message = record.getMessage()
        assert "stage=app_state_injection" in message
        assert "exc=AttributeError" in message
        assert getattr(record, "stage", None) == "app_state_injection"
        assert getattr(record, "root_exception_type", None) == "AttributeError"
        assert record.exc_info is not None

    @pytest.mark.asyncio
    async def test_cancelled_startup_is_logged_and_still_propagates(self):
        """``asyncio.CancelledError``（BaseException 分支）同样记录并传播。

        启动期间被取消（例如滚动重启打断）必须留下痕迹，而不是静默退出 ——
        只捕获 ``Exception`` 的实现会漏掉这一类。
        """
        from unittest.mock import patch

        import api.app_factory as factory_mod

        exc = asyncio.CancelledError("startup cancelled by supervisor")
        with (
            _capture_app_factory_logs() as records,
            patch.object(factory_mod, "_container", _ExplodingContainer(exc)),
            pytest.raises(asyncio.CancelledError),
        ):
            async with factory_mod.lifespan(_mock_app()):
                pytest.fail("取消后绝不能 yield")

        criticals = [r for r in records if r.levelno >= logging.CRITICAL]
        assert criticals, "CancelledError 也必须落 CRITICAL 日志"
        assert "exc=CancelledError" in criticals[-1].getMessage()


@pytest.mark.unit
class TestAlembicMustNotDisableApplicationLoggers:
    """`init_db()` 不得关掉应用自己的 logger（issue #50 的第二个根因）。

    `alembic/env.py` 里的 `fileConfig()` 若用默认的
    `disable_existing_loggers=True`，会把此刻已存在、但未被 `alembic.ini`
    声明的 logger 全部置为 `disabled = True`。`api/app_factory.py` 的
    `"app_factory"` logger（第 23 行）正好早于第 28 行的 `init_db()`，
    于是该模块的**所有**日志——包括"启动失败"这条最不能丢的——都会在
    `isEnabledFor()` 处直接返回，连 stderr 都不写。

    这里用 AST 断言源码契约（不执行 alembic 运行时），与
    `tests/unit/test_mcp_metrics_contract.py` 同一手法；端到端的行为证据在
    `TestLifespanStartupFailureUnderUvicorn`（runner 真实导入
    `api.app_factory`，会走到 `init_db()`）。
    """

    ENV_PATH = REPO_ROOT / "alembic" / "env.py"

    def _fileconfig_kwargs(self) -> list[ast.Call]:
        tree = ast.parse(self.ENV_PATH.read_text(encoding="utf-8"))
        return [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "fileConfig"
        ]

    def test_fileconfig_is_called(self):
        calls = self._fileconfig_kwargs()
        assert calls, f"{self.ENV_PATH} 必须调用 fileConfig 配置 alembic 自己的日志"

    def test_fileconfig_disables_no_existing_loggers(self):
        for call in self._fileconfig_kwargs():
            kwargs = {kw.arg: kw.value for kw in call.keywords}
            assert "disable_existing_loggers" in kwargs, (
                "fileConfig 必须显式传 disable_existing_loggers；"
                "该参数默认值是 True（且 fileConfig 不读 alembic.ini 里的同名键，"
                "那是 dictConfig 的行为），会把应用已装配的 logger 全部禁用"
            )
            value = kwargs["disable_existing_loggers"]
            assert isinstance(value, ast.Constant) and value.value is False, (
                "disable_existing_loggers 必须是字面量 False"
            )


@pytest.mark.unit
class TestLogStartupFailure:
    """``core.logger.log_startup_failure`` 自身的契约。"""

    def test_json_formatter_carries_exception_and_structured_fields(self):
        """生产 JSON 日志面必须同时带 traceback 与结构化失败字段。"""
        from core.logger import _JSONFormatter, log_startup_failure

        logger, records = _isolated_logger("startup_failure_json")
        log_startup_failure(
            "container.initialize",
            _startup_failure_exc(),
            "reboot after fixing",
            logger=logger,
        )

        payload = json.loads(_JSONFormatter().format(records[-1]))
        assert payload["level"] == "CRITICAL"
        assert payload["extra"]["event"] == "lifespan_startup_failed"
        assert payload["extra"]["phase"] == "startup"
        assert payload["extra"]["stage"] == "container.initialize"
        assert payload["extra"]["exception_type"] == "ConfigurationError"
        assert payload["extra"]["root_exception_type"] == "ConnectionError"
        assert "Traceback (most recent call last)" in payload["exception"]
        assert "ConnectionError" in payload["exception"]

    def test_logs_traceback_even_when_called_outside_except_block(self):
        """显式 exc_info 三元组：脱离 except 块也必须拿到真实 traceback。"""
        from core.logger import log_startup_failure

        logger, records = _isolated_logger("startup_failure_no_except")
        # 异常在本行构造并返回，调用点已不在任何 except 块内。
        log_startup_failure("stage.x", _startup_failure_exc(), "hint", logger=logger)

        record = records[-1]
        assert record.exc_info is not None
        rendered = logging.Formatter().formatException(record.exc_info)
        assert "Traceback (most recent call last)" in rendered
        assert "ConfigurationError" in rendered

    def test_shutdown_phase_event_name_differs(self):
        from core.logger import log_startup_failure

        logger, records = _isolated_logger("startup_failure_shutdown_phase")
        log_startup_failure(
            "container.close",
            _startup_failure_exc(),
            "hint",
            logger=logger,
            phase="shutdown",
        )
        assert records[-1].event == "lifespan_shutdown_failed"  # type: ignore[attr-defined]
        assert records[-1].phase == "shutdown"  # type: ignore[attr-defined]

    def test_root_cause_walk_terminates_on_cyclic_chain(self):
        """异常链成环时不得死循环（否则失败路径上再挂一个更隐蔽的故障）。"""
        from core.logger import _root_cause

        first = ValueError("first")
        second = ValueError("second")
        first.__cause__ = second
        second.__cause__ = first

        # 环被切断在最后一个"未重复"的节点上 —— 无需深究具体返回谁，
        # 这里钉死的是"必须终止且不能抛异常"。
        assert _root_cause(first) in (first, second)
        assert _root_cause(second) in (first, second)

    def test_root_cause_returns_self_for_unchained_exception(self):
        from core.logger import _root_cause

        solo = ValueError("solo")
        assert _root_cause(solo) is solo

    def test_root_cause_follows_context_when_no_cause(self):
        from core.logger import _root_cause

        try:
            try:
                raise KeyError("inner")
            except KeyError:
                raise RuntimeError("outer")  # noqa: B904 — 刻意用隐式 __context__
        except RuntimeError as outer:
            assert _root_cause(outer).__class__ is KeyError


@pytest.mark.integration
class TestLifespanStartupFailureUnderUvicorn:
    """真实 uvicorn 子进程：exit != 0 / 根因在日志 / traceback / 未静默成功。"""

    @staticmethod
    def _run(outdir: Path, stage: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-W", "ignore", str(RUNNER), str(outdir), stage],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_container_initialize_failure_exits_nonzero_and_logs_root_cause(self, tmp_path: Path):
        """容器初始化失败：非零退出 + 日志含根因与 traceback + 未静默成功。"""
        outdir = tmp_path / "initialize"
        proc = self._run(outdir, "initialize")

        assert proc.returncode != 0, (
            f"启动失败必须非零退出，实际 {proc.returncode}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )

        for stream_name, text in (
            ("stderr", proc.stderr),
            ("logs/app.log", _app_log(outdir)),
        ):
            assert _EVENT_MARKER in text, f"{stream_name} 缺少启动失败事件标记"
            assert "stage=container.initialize" in text
            assert "exc=ConfigurationError" in text, f"{stream_name} 缺少失败异常类型"
            assert "根因=ConnectionError" in text, f"{stream_name} 缺少根因异常类型"
            assert "connection refused" in text, f"{stream_name} 缺少根因消息"
            assert "Traceback (most recent call last)" in text, f"{stream_name} 缺少 traceback"
            assert "The above exception was the direct cause of" in text, (
                f"{stream_name} 的 traceback 缺少异常链根因段"
            )
            assert _SUCCESS_LOG_MARKER not in text, f"{stream_name} 出现成功启动日志（静默成功）"

        assert _UVICORN_STARTUP_COMPLETE not in proc.stderr, (
            "uvicorn 报告 startup complete —— 启动失败却被当作成功"
        )

    def test_app_state_injection_failure_exits_nonzero_and_logs_its_stage(self, tmp_path: Path):
        """注入阶段失败：同样非零退出，且 stage 精确指向注入阶段。"""
        outdir = tmp_path / "injection"
        proc = self._run(outdir, "injection")

        assert proc.returncode != 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

        text = _app_log(outdir)
        assert "stage=app_state_injection" in text
        assert "exc=AttributeError" in text
        assert "Traceback (most recent call last)" in text
        assert _SUCCESS_LOG_MARKER not in text


def _app_log(outdir: Path) -> str:
    """读取 runner 隔离目录下的项目轮转日志（缺失即视为"未落盘"）。"""
    log_path = outdir / "logs" / "app.log"
    assert log_path.exists(), f"项目轮转日志未生成: {log_path}"
    return log_path.read_text(encoding="utf-8")
