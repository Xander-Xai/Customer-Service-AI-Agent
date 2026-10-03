"""HTTP client 生命周期回归测试 —— issue #44（``rag/api_embedding.py``）。

**旧实现为什么坏**

``ApiEmbedding.encode()`` 在「当前线程没有运行中的事件循环」时会走
``asyncio.run(self.aencode(...))``。这正是
``rag/qdrant_knowledge_base.py::retrieve``（``loop.run_in_executor(None, ...)``）
和 ``scripts/import_eval_corpus.py``（``asyncio.to_thread``）的形状：
``run_in_executor`` 给的是**没有运行 loop 的工作线程**，所以每次同步 encode
都会新建一个一次性 loop、把模块级 ``httpx.AsyncClient`` 绑上去，然后
``asyncio.run`` 在返回时把该 loop 关掉。

``_get_async_client()`` 只在 ``_async_client.is_closed`` 为真时重建，而
「loop 已死」的 client 永远不是 closed 状态 —— 于是这个绑在死 loop 上的
连接池被无限复用，下一次落在别的 loop 上就抛
``RuntimeError: Event loop is closed``。调用方用宽 ``except Exception`` 把它
降级成 ``vector_status="embedding_unavailable"``（仅 WARNING），所以 RAG
向量通道是被静默关掉的。

**为什么 stub 必须支持 keep-alive**

httpx 的连接池只有在真的持有可复用的长连接时才需要 loop 相关状态。
HTTP/1.0 ``Connection: close`` 的 stub 每次都重新建连，旧实现反而「碰巧」
不报错 —— 那种 stub 复现不出这个 bug。真实 provider（siliconflow / OpenAI）
是 HTTP/1.1 keep-alive，所以下面所有 stub 都是 HTTP/1.1。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from rag import api_embedding
from rag.api_embedding import ApiEmbedding, close_async_client

DIM = 8
LOOP_LIFECYCLE_MARKER = "Event loop is closed"


class _StubHandler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容 embedding stub（HTTP/1.1 keep-alive）。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        status = int(self.server.provider_status)  # type: ignore[attr-defined]
        if status >= 400:
            payload: dict[str, Any] = {"error": {"message": "stub provider error"}}
        else:
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:  # 静音 access log
        pass


class _StubProvider:
    """可切换状态码的本地 provider（默认 200）。"""

    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.httpd.daemon_threads = True
        self.httpd.provider_status = 200  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def set_status(self, status: int) -> None:
        self.httpd.provider_status = status  # type: ignore[attr-defined]

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
async def _isolated_async_client_registry() -> AsyncIterator[None]:
    """每个用例前后都清空模块级 async client 注册表，避免跨用例污染。"""
    await close_async_client()
    yield
    await close_async_client()


def _owner_loop() -> Any:
    """取模块级 async client 的归属 loop。

    旧实现没有这个属性，用 getattr 拿 None，好让断言报出可读的原因而不是
    AttributeError。
    """
    return getattr(api_embedding, "_async_client_loop", None)


def _fail_if_loop_lifecycle_error(exc: BaseException) -> None:
    """把 transport 生命周期错误显式判成失败，并给出可操作的说明。"""
    chain: list[str] = []
    cur: BaseException | None = exc
    while cur is not None:
        chain.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    joined = " | ".join(chain)
    assert LOOP_LIFECYCLE_MARKER not in joined, (
        "跨 event loop 复用了 loop-bound httpx.AsyncClient："
        f"{joined}。同步 encode() 必须在调用内自建自销同步 client，"
        "不得用 asyncio.run() 起一次性 loop 去借用模块级 async client。"
    )


async def _expect_no_loop_error(awaitable: Any, what: str) -> Any:
    try:
        return await awaitable
    except Exception as exc:  # noqa: BLE001 - 这里要区分异常种类
        _fail_if_loop_lifecycle_error(exc)
        raise AssertionError(f"{what} 期望成功，但抛出了 {type(exc).__name__}: {exc}") from exc


# ── requirement 4：worker/thread 同步 encode 之后，主 loop 的 async encode ──


async def test_thread_sync_encode_then_main_loop_async_encode(
    embedding: ApiEmbedding,
) -> None:
    """issue #44 的原始顺序：先 run_in_executor 同步 encode，再主 loop aencode。"""
    loop = asyncio.get_running_loop()

    batch = await _expect_no_loop_error(
        loop.run_in_executor(None, embedding.encode, ["hello"]),
        "worker-thread sync encode",
    )
    assert batch.shape == (1, DIM)

    vector = await _expect_no_loop_error(embedding.aencode("hello"), "main-loop async aencode")
    assert vector.shape == (DIM,)


async def test_repeated_thread_encode_cycles_never_leak_loop_bound_client(
    embedding: ApiEmbedding,
) -> None:
    """多轮「线程同步 encode ↔ 主 loop async encode」交替，两条路径都必须一直可用。"""

    for _ in range(3):
        batch = await _expect_no_loop_error(
            asyncio.to_thread(embedding.encode, ["轮询"]),
            "to_thread sync encode",
        )
        assert batch.shape == (1, DIM)

        vector = await _expect_no_loop_error(embedding.aencode("轮询"), "main-loop async aencode")
        assert vector.shape == (DIM,)


async def test_concurrent_thread_encodes_from_many_threads(
    embedding: ApiEmbedding,
) -> None:
    """多个工作线程并发同步 encode（导入脚本形态）后，主 loop 仍能 async encode。"""
    loop = asyncio.get_running_loop()

    futures = [loop.run_in_executor(None, embedding.encode, [f"q{i}"]) for i in range(4)]
    for fut in futures:
        try:
            await fut
        except Exception as exc:  # noqa: BLE001
            _fail_if_loop_lifecycle_error(exc)
            raise

    vector = await _expect_no_loop_error(embedding.aencode("hello"), "main-loop async aencode")
    assert vector.shape == (DIM,)


# ── requirement 2/3：client 所有权与生命周期 ──


async def test_sync_encode_never_borrows_the_module_async_client(
    embedding: ApiEmbedding,
) -> None:
    """同步 encode 不得触碰 async client 注册表（它的 client 生命周期 = 单次调用）。"""
    assert api_embedding._async_client is None
    assert _owner_loop() is None

    batch = await _expect_no_loop_error(
        asyncio.to_thread(embedding.encode, ["hello"]), "to_thread sync encode"
    )
    assert batch.shape == (1, DIM)

    assert api_embedding._async_client is None, (
        "同步路径借用了模块级 httpx.AsyncClient —— 它是 loop-bound 的，"
        "必须只由 aencode() 在当前运行 loop 上创建"
    )
    assert _owner_loop() is None


def test_aencode_under_two_short_lived_loops_uses_two_distinct_clients(
    embedding: ApiEmbedding,
) -> None:
    """两个先后销毁的 loop 各自必须拿到自己的 client，绝不复用上一个 loop 的。"""

    def _one_call() -> None:
        vector = asyncio.run(embedding.aencode("hello"))
        assert vector.shape == (DIM,)

    _one_call()
    first_client = api_embedding._async_client
    first_loop = _owner_loop()
    assert first_client is not None
    assert first_loop is not None, (
        "模块级 async client 没有记录归属事件循环 —— 无法保证它不会被交给另一个 loop 复用"
    )
    assert first_loop.is_closed()

    _one_call()
    second_client = api_embedding._async_client
    second_loop = _owner_loop()
    assert second_client is not first_client, (
        "第二个 loop 拿到了第一个（已销毁）loop 的 AsyncClient"
    )
    assert second_loop is not first_loop


async def test_async_client_is_reused_within_one_loop(
    embedding: ApiEmbedding,
) -> None:
    """同一个 loop 内仍然复用连接池（v6.3 的优化不能被这次修复顺带废掉）。"""
    for _ in range(3):
        await _expect_no_loop_error(embedding.aencode("hello"), "aencode")
    assert api_embedding._async_client is not None
    assert _owner_loop() is asyncio.get_running_loop()


async def test_close_async_client_is_idempotent(embedding: ApiEmbedding) -> None:
    await embedding.aencode("hello")
    await close_async_client()
    await close_async_client()
    assert api_embedding._async_client is None
    assert _owner_loop() is None


def test_close_async_client_from_a_foreign_loop_does_not_raise(
    embedding: ApiEmbedding,
) -> None:
    """归属 loop 已销毁后，shutdown 路径不得再抛 Event loop is closed。

    `aclose()` 会顺着连接池去关 socket，而 socket 的 transport 记着自己创建
    时的那个 loop；对已销毁的 loop 调 `aclose()` 会在 loop 内部抛
    `RuntimeError: Event loop is closed`，把应用关闭 / 测试清理变成崩溃。
    """
    asyncio.run(embedding.aencode("hello"))  # client 归属这条已销毁的 loop
    assert _owner_loop() is not None
    assert _owner_loop().is_closed()

    asyncio.run(close_async_client())  # 在另一条 loop 上执行关闭
    assert api_embedding._async_client is None
    assert _owner_loop() is None


# ── requirement 5：provider 错误不得被伪装成 loop 错误 ──


async def test_provider_401_stays_a_provider_error_on_async_path(
    provider: _StubProvider, embedding: ApiEmbedding
) -> None:
    provider.set_status(401)
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await embedding.aencode("hello")
    _fail_if_loop_lifecycle_error(excinfo.value)
    assert excinfo.value.response.status_code == 401


async def test_provider_401_stays_a_provider_error_on_sync_path(
    provider: _StubProvider, embedding: ApiEmbedding
) -> None:
    provider.set_status(401)
    loop = asyncio.get_running_loop()
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await loop.run_in_executor(None, embedding.encode, ["hello"])
    _fail_if_loop_lifecycle_error(excinfo.value)
    assert excinfo.value.response.status_code == 401


async def test_provider_500_stays_a_provider_error_after_a_thread_encode(
    provider: _StubProvider, embedding: ApiEmbedding
) -> None:
    """先跑一次成功同步 encode，再让 provider 500：必须是 provider 错误。"""
    loop = asyncio.get_running_loop()
    await _expect_no_loop_error(
        loop.run_in_executor(None, embedding.encode, ["hello"]),
        "worker-thread sync encode",
    )

    provider.set_status(500)
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await embedding.aencode("hello")
    _fail_if_loop_lifecycle_error(excinfo.value)
    assert excinfo.value.response.status_code == 500


async def test_provider_401_after_thread_encode_does_not_crash_the_next_call(
    provider: _StubProvider, embedding: ApiEmbedding
) -> None:
    """一次 provider 401 之后，async client 仍必须健康（不得留下半死状态）。"""
    loop = asyncio.get_running_loop()
    provider.set_status(401)
    with pytest.raises(httpx.HTTPStatusError):
        await loop.run_in_executor(None, embedding.encode, ["hello"])

    provider.set_status(200)
    vector = await _expect_no_loop_error(embedding.aencode("hello"), "aencode after provider 401")
    assert vector.shape == (DIM,)


# ── requirement 6：向量语义不变 ──


async def test_vector_values_and_shapes_are_unchanged(
    embedding: ApiEmbedding,
) -> None:
    """按 index 排序、单条 1D / 多条 2D、float32 —— 与修复前一致。"""
    single = await embedding.aencode("hello")
    assert single.shape == (DIM,)
    assert single.dtype.name == "float32"
    assert single.tolist() == pytest.approx([0.1] * DIM)

    multi = await embedding.aencode(["a", "b", "c"])
    assert multi.shape == (3, DIM)
    assert multi.dtype.name == "float32"
    for row, expected in enumerate((0.1, 0.2, 0.3)):
        assert multi[row].tolist() == pytest.approx([expected] * DIM)

    loop = asyncio.get_running_loop()
    sync_multi = await _expect_no_loop_error(
        loop.run_in_executor(None, embedding.encode, ["a", "b", "c"]),
        "sync encode",
    )
    assert sync_multi.shape == (3, DIM)
    assert sync_multi.dtype.name == "float32"
    for row in range(3):
        assert sync_multi[row].tolist() == pytest.approx(multi[row].tolist())
