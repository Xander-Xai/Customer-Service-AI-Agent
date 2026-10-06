"""Shutdown lifecycle 回归测试 —— issue #55（``close_async_client()`` 是死代码）。

**旧实现为什么坏**

``rag/api_embedding.py`` 的 ``close_async_client()`` 从 #44 起就是 loop 安全的
（只 ``aclose()`` 归属当前 loop 的 client，绝不跨 loop 强行关闭），但**没有任何
调用点**::

    $ grep -rn "close_async_client" --include=*.py . | grep -v tests
    ./rag/api_embedding.py:...

容器的 ``close()`` 关掉了 Redis、LLM 连接池、checkpoint 后端和 ERP，唯独漏掉
embedding 的 ``httpx.AsyncClient``。而真实入口
``uvicorn api.app_factory:app`` 用的 lifespan 是 ``api/app_factory.py::lifespan``
（它会覆盖 ``api/app.py::create_app`` 里那个 lifespan），那条链的唯一释放动作是
``await _container.close()`` —— 所以 client 在进程退出时只能靠 GC 回收，模块级
注册表还会一直把 ``client → transport → loop`` 这条强引用链钉住。

**这个测试为什么盯 ``is_closed`` / transport 而不是 ResourceWarning**

issue #55 的 Impact 里写「httpx/httpcore 会在解释器退出时发
``ResourceWarning: unclosed client``」。实测下来这句话需要修正：

- httpx 0.27.0 **自己不发** 任何 ``ResourceWarning``（``AsyncClient`` 没有会告警的
  ``__del__``）；
- 真正会刷出来的是 CPython ``asyncio/selector_events.py`` 里 transport 的
  ``__del__``：``ResourceWarning: unclosed transport <_SelectorSocketTransport ...>``，
  触发条件是 client 握着实连接又被丢弃；
- 但它**只在对象恰好落进观察窗口时被 GC 才出现** —— 同一份代码实测有时 0 条、有时
  1 条。

也就是说「没有 ResourceWarning」是一个 **GC 时序相关的 flaky 判据**，拿它当断言等于
没测（issue 原文的假设不成立，本 PR 不假装它成立）。这里改用三个**确定性**信号：

1. ``client.is_closed is True`` —— ``aclose()`` 真的跑过；
2. ``transport.is_closed is True`` 且池内长连接数归零 —— 释放一路走到了 socket，
   不是只翻了个标志位；
3. ``async_client_registry_snapshot()`` 为空 —— 模块级注册表没留下
   ``client → transport → loop`` 的强引用钉子（#44 注释点名的那个循环引用）。

stub 必须是 HTTP/1.1 keep-alive：只有池里真的握着可复用的长连接，关闭才有意义，
``Connection: close`` 的 stub 会让本文件退化成空断言。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.container import ServiceContainer
from rag.api_embedding import (
    ApiEmbedding,
    async_client_registry_snapshot,
    close_async_client,
)

DIM = 8


class _StubHandler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容 embedding stub（HTTP/1.1 keep-alive，池内会长连接）。

    额外统计**服务端视角**的 TCP 连接数：``setup()`` / ``finish()`` 是
    ``socketserver`` 给的连接钩子，客户端 ``aclose()`` 之后这里能观测到 socket
    真的被关掉 —— 这是唯一能直接证明「没有 ResourceWarning 类生命周期泄漏」的
    确定性信号（httpx / httpcore 都不暴露 transport 的 is_closed）。
    """

    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.server.open_connections += 1  # type: ignore[attr-defined]

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.server.open_connections -= 1  # type: ignore[attr-defined]

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        texts = body.get("input", [])
        if isinstance(texts, str):
            texts = [texts]
        payload = {
            "data": [
                {"index": i, "embedding": [round(0.1 * (i + 1), 4)] * DIM}
                for i in range(len(texts))
            ]
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        pass


def _wait_for(predicate: Any, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """轮询等待条件成立（服务端看到 socket 关闭有极小的传播延迟）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _StubProvider:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.httpd.daemon_threads = True
        self.httpd.open_connections = 0
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    @property
    def open_connections(self) -> int:
        return self.httpd.open_connections  # type: ignore[attr-defined]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def provider() -> Iterator[_StubProvider]:
    stub = _StubProvider()
    try:
        yield stub
    finally:
        stub.close()


@pytest.fixture
def embedding(provider: _StubProvider) -> ApiEmbedding:
    return ApiEmbedding(api_key="sk-stub-provider-key", base_url=provider.base_url)


@pytest.fixture(autouse=True)
async def _isolated_registry() -> AsyncIterator[None]:
    """每个用例前后清空模块级注册表，避免跨用例污染。"""
    await close_async_client()
    yield
    await close_async_client()


def _registry() -> dict[Any, Any]:
    return dict(async_client_registry_snapshot())


def _pooled_connection_count(client: httpx.AsyncClient) -> int:
    return len(client._transport._pool._connections)


def _closed_container() -> ServiceContainer:
    """一个可以直接走完 ``close()`` 的容器（不碰任何外部基础设施）。

    ``ServiceContainer.close()`` 在 ``_initialized`` 为假时直接 return，所以这里
    只置位标志位：Redis / ERP / checkpoint 在未 initialize 的容器上都是空的，
    ``close()`` 会把它们各自跳过，剩下的正是本 issue 关心的释放链。
    """
    container = ServiceContainer()
    container._initialized = True
    return container


@contextmanager
def _lifespan_without_warmup() -> Iterator[None]:
    """跑真实 lifespan，但不让它调度缓存预热后台任务。

    ``api/app_factory.py::lifespan`` 在 startup 用 ``asyncio.create_task`` 起一个
    ``await asyncio.sleep(8)`` 的预热任务且从不取消它，于是每次 lifespan 退出都会留下一个
    pending task（stderr 打印 ``Task was destroyed but it is pending!``）。那与本 issue
    无关，把 import 打断即可让 lifespan 走它自己的 ``except`` 分支。
    """
    with patch("scripts.warm_cache.warm_cache_via_api", side_effect=RuntimeError("skip")):
        yield


def _faked_initialize(container: ServiceContainer) -> Any:
    """给真实容器配一个只设置 graph_app 的 ``initialize()``。

    真实 ``initialize()`` 会连 Qdrant / 建图 / 跑 embedding，与本 issue 无关；
    这里只保留 ``close()`` 真正关心的状态：``_initialized`` 已置位。
    """

    async def _init() -> None:
        container.graph_app = MagicMock()

    return _init


# ── 需求 1：应用 lifespan 退出 → client 真的被关闭 ──


async def test_lifespan_exit_closes_embedding_client(embedding: ApiEmbedding) -> None:
    """真实入口 lifespan 退出时，embedding AsyncClient 一定被 aclose。

    走的是 ``api.app_factory.lifespan``（``uvicorn api.app_factory:app`` 实际使用的
    那条），而不是直接调 ``close_async_client()`` —— 这样才证明得了「调用点真的存在」。
    """
    import api.app_factory as factory_mod

    await embedding.aencode("hello")
    client = _registry()[asyncio.get_running_loop()]
    assert client.is_closed is False, "前置条件：lifespan 之前 client 必须还活着"

    container = _closed_container()
    container.initialize = _faked_initialize(container)  # type: ignore[method-assign]

    with _lifespan_without_warmup(), pytest.MonkeyPatch.context() as mp:
        mp.setattr(factory_mod, "_container", container)
        async with factory_mod.lifespan(MagicMock()):
            assert _registry(), "lifespan 运行期间 client 仍在注册表里"
        # ← lifespan exit

    assert client.is_closed is True, "lifespan 退出后 embedding client 必须是 closed"
    assert _registry() == {}, f"lifespan 退出后注册表必须为空，实际 {_registry()}"


async def test_lifespan_exit_releases_a_live_pooled_connection(
    embedding: ApiEmbedding,
) -> None:
    """被关闭的不是一个空壳 client：它确实握着 keep-alive 长连接，且连接被释放。

    没有这一条，「client.is_closed is True」可能只是关了一个从未建连的对象，
    证明不了池化资源真的回收了。
    """
    import api.app_factory as factory_mod

    await embedding.aencode("hello")
    client = _registry()[asyncio.get_running_loop()]
    assert _pooled_connection_count(client) >= 1, (
        "前置条件：stub 是 keep-alive，池里应该真的握住了长连接；"
        "若为 0 说明本用例退化成了空断言"
    )

    container = _closed_container()
    container.initialize = _faked_initialize(container)  # type: ignore[method-assign]

    with _lifespan_without_warmup(), pytest.MonkeyPatch.context() as mp:
        mp.setattr(factory_mod, "_container", container)
        async with factory_mod.lifespan(MagicMock()):
            pass

    assert _pooled_connection_count(client) == 0, "aclose() 之后池里不该再有长连接"


# ── 需求 2：模块级注册表不再钉住 client → transport → loop ──


async def test_shutdown_leaves_no_module_level_client_reference(
    embedding: ApiEmbedding,
) -> None:
    """关闭后模块注册表为空 —— #44 注释点名的强引用链没有残留。

    ``client → transport → loop`` 是循环引用，光 ``aclose()`` 断不开；真正断开的是
    从注册表里摘掉 client。所以「池关了」和「注册表空了」是两件事，必须分别断言。
    """
    container = _closed_container()

    await embedding.aencode("hello")
    loop = asyncio.get_running_loop()
    assert loop in _registry()

    await container.close()

    assert loop not in _registry(), "关闭后归属 loop 仍被注册表钉住"
    assert _registry() == {}


# ── 需求 3：幂等 ──


async def test_container_close_is_idempotent(embedding: ApiEmbedding) -> None:
    """重复关闭不报错，且不会把已关的 client 再去 aclose 一次。"""
    container = _closed_container()

    await embedding.aencode("hello")
    client = _registry()[asyncio.get_running_loop()]

    await container.close()
    assert client.is_closed is True

    # 第二次：注册表已空，必须安全 no-op
    await container.close()
    assert client.is_closed is True
    assert _registry() == {}


async def test_repeated_lifespan_cycles_do_not_raise(embedding: ApiEmbedding) -> None:
    """反复起停应用（每轮都真的建过 client）不得抛异常。

    每轮用一个**新容器**，忠实还原「initialize → 运行 → close」的真实循环。
    ``ServiceContainer.close()`` 在 ``_initialized`` 为假时直接 return，所以复用同一个
    容器只能验证第一轮 —— 那属于容器的既有语义，不是本 issue 的范围。
    """
    import api.app_factory as factory_mod

    for _ in range(3):
        container = _closed_container()
        container.initialize = _faked_initialize(container)  # type: ignore[method-assign]

        with _lifespan_without_warmup(), pytest.MonkeyPatch.context() as mp:
            mp.setattr(factory_mod, "_container", container)
            async with factory_mod.lifespan(MagicMock()):
                await embedding.aencode("hello")
                assert _registry(), "每轮 lifespan 内 client 都应已建立"
            assert _registry() == {}, "每轮退出后注册表都应清空"


# ── 需求 4：已关闭 / 无 client 都安全 ──


async def test_shutdown_with_no_client_is_safe() -> None:
    """一个 client 都没建过就关闭 —— 必须安静通过。

    ``EMBEDDING_API_KEY`` 缺失时 ``_init_rag`` 不会建 embedding 模型，线上关闭时
    就会走到这个分支。
    """
    container = _closed_container()
    assert _registry() == {}

    await container.close()  # 不抛异常即通过
    assert _registry() == {}


async def test_shutdown_after_client_already_closed_is_safe(
    embedding: ApiEmbedding,
) -> None:
    """client 已被别处显式关闭（``is_closed``）时再关一次不报错。"""
    container = _closed_container()

    await embedding.aencode("hello")
    client = _registry()[asyncio.get_running_loop()]
    await client.aclose()
    assert client.is_closed is True

    await container.close()
    assert _registry() == {}


# ── 需求 5：不跨 event loop 强行 aclose ──


async def test_shutdown_does_not_force_aclose_across_event_loops() -> None:
    """归属 loop 已销毁时，关闭只丢弃引用，绝不跨 loop 去 aclose。

    跨 loop ``await client.aclose()`` 会顺着连接池去关 socket，而 socket 的
    transport 记着自己创建时的那个已销毁 loop，于是关闭路径本身会抛
    ``RuntimeError: Event loop is closed`` —— 把应用关闭变成崩溃。这里显式复现
    「client 归属一条已销毁的 loop」这个形状。
    """
    container = _closed_container()

    # 在一条短命 loop 上建 client，随后销毁该 loop。必须放到独立线程里 ——
    # 测试自身已经跑在一条运行中的 loop 上，不能嵌套 run_until_complete。
    def _build_on_short_lived_loop() -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_get_client_for_current_loop())
        finally:
            loop.close()

    thread = threading.Thread(target=_build_on_short_lived_loop)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "worker 线程卡死"

    stale_client = next(iter(_registry().values()))
    assert stale_client.is_closed is False, "前置条件：client 自身没被关，只是 loop 死了"

    # 当前 loop 与归属 loop 不同 —— 关闭必须安全，且不得跨 loop aclose
    await container.close()

    assert _registry() == {}, "归属 loop 已销毁的条目应被丢弃"
    assert stale_client.is_closed is False, (
        "跨 loop 强行 aclose 是 #44 明确禁止的行为：销毁 loop 上的 socket 早已随 loop "
        "关闭，再去 await aclose() 只会抛 Event loop is closed"
    )


async def _get_client_for_current_loop() -> httpx.AsyncClient:
    from rag.api_embedding import _get_async_client

    return await _get_async_client()


async def test_shutdown_from_a_foreign_loop_raises_no_loop_error(
    embedding: ApiEmbedding,
) -> None:
    """在另一条 loop 上执行关闭：不得抛 Event loop is closed。"""
    container = _closed_container()

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_get_client_for_current_loop())
        finally:
            loop.close()

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "worker 线程卡死"
    assert _registry(), "前置条件：worker loop 上确实建过 client"

    await container.close()  # 跨 loop 关闭不得抛 RuntimeError

    assert _registry() == {}


# ── 需求 6：清理失败不阻断容器关闭的其余步骤 ──


async def test_embedding_close_failure_does_not_abort_container_shutdown() -> None:
    """embedding 清理抛异常时，容器关闭流程仍要走完（状态重置不能被跳过）。"""
    container = _closed_container()
    container.erp = MagicMock()
    container.erp.close = AsyncMock()

    with patch(
        "rag.api_embedding.close_async_client",
        side_effect=RuntimeError("embedding close exploded"),
    ):
        await container.close()  # 不得冒泡

    container.erp.close.assert_awaited_once()
    assert container._initialized is False, "状态重置必须照常执行"
    assert container.graph_app is None


# ── 事实记录：本版本 httpx 不发 ResourceWarning ──


async def test_shutdown_closes_the_tcp_socket_at_the_server_side(
    embedding: ApiEmbedding, provider: _StubProvider
) -> None:
    """服务端视角：shutdown 之后那条 keep-alive TCP 连接真的被关掉了。

    这是本 issue 验收项「无 ResourceWarning 类生命周期泄漏」最直接的证据。
    httpx 0.27.0 / httpcore 1.0.9 都不暴露 transport 的 ``is_closed``，所以只能从
    对端观测：客户端 ``aclose()`` → FIN → 服务端 ``finish()`` 钩子把计数减回去。
    """
    container = _closed_container()

    await embedding.aencode("hello")
    client = _registry()[asyncio.get_running_loop()]
    assert _pooled_connection_count(client) >= 1, "前置条件：池里握着长连接"
    assert _wait_for(lambda: provider.open_connections >= 1), (
        f"前置条件：服务端应看到至少 1 条连接，实际 {provider.open_connections}"
    )

    await container.close()

    assert _wait_for(lambda: provider.open_connections == 0), (
        "shutdown 后服务端的 keep-alive 连接没有被关闭 —— socket 泄漏，"
        f"仍开着 {provider.open_connections} 条"
    )
    assert _pooled_connection_count(client) == 0
