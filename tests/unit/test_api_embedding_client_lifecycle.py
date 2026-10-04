"""HTTP client 生命周期回归测试 —— issue #44（``rag/api_embedding.py``）。

**旧实现为什么坏**

``ApiEmbedding.encode()`` 在「当前线程没有运行中的事件循环」时会走
``asyncio.run(self.aencode(...))``。这正是
``rag/qdrant_knowledge_base.py::retrieve``（``loop.run_in_executor(None, ...)``）
和 ``scripts/import_eval_corpus.py``（``asyncio.to_thread``）的形状：
``run_in_executor`` 给的是**没有运行 loop 的工作线程**，所以每次同步 encode
都会新建一个一次性 loop、把模块级 ``httpx.AsyncClient`` 绑上去，然后
``asyncio.run`` 在返回时把该 loop 关掉。

第一版修复只留了一对模块级全局 ``_async_client`` / ``_async_client_loop``：
「归属 loop ≠ 当前 loop 就重建」。这治住了跨 loop 复用，却把 last-loop-wins
带进来了 —— 两条同时存活的 loop 交替调用时，每次调用都会把对方**仍在运行**的
client 顶掉（PR #54 review：*Preserve one client for each live event loop*），
被顶掉的 client 既没关闭也没人再引用，连接池资源与 keep-alive 连接一起泄漏，
连接池复用也等于没了。

现在的分区表以**归属 loop** 为键（``_clients_by_loop``），每条存活 loop 各持
一个 client，A→B→A 的交替不会重建 A 的 client。

**为什么 stub 必须支持 keep-alive**

httpx 的连接池只有在真的持有可复用的长连接时才需要 loop 相关状态。
HTTP/1.0 ``Connection: close`` 的 stub 每次都重新建连，旧实现反而「碰巧」
不报错 —— 那种 stub 复现不出这个 bug。真实 provider（siliconflow / OpenAI）
是 HTTP/1.1 keep-alive，所以下面所有 stub 都是 HTTP/1.1。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from rag import api_embedding
from rag.api_embedding import (
    ApiEmbedding,
    async_client_registry_snapshot,
    close_async_client,
)

DIM = 8
LOOP_LIFECYCLE_MARKER = "Event loop is closed"
FOREIGN_LOOP_MARKER = "bound to a different event loop"


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


def _registry() -> dict[Any, Any]:
    """分区表快照：``{归属 loop: client}``。"""
    return dict(async_client_registry_snapshot())


def _only_client() -> httpx.AsyncClient:
    """分区表里唯一的那个 client（多于一个就说明分区失效）。"""
    entries = async_client_registry_snapshot()
    assert len(entries) == 1, f"期望分区表恰好 1 个 client，实际 {len(entries)} 个：{entries}"
    return entries[0][1]


def _only_owner() -> Any:
    """分区表里唯一的那个归属 loop。"""
    entries = async_client_registry_snapshot()
    assert len(entries) == 1, f"期望分区表恰好 1 个归属 loop，实际 {len(entries)} 个：{entries}"
    return entries[0][0]


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
    assert FOREIGN_LOOP_MARKER not in joined, (
        f"同一 loop 的连接池被交给了另一条 event loop：{joined}。"
        "async client 必须按归属 loop 分区存放，而不是全局单例。"
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
    """同步 encode 不得触碰 async client 分区表（它的 client 生命周期 = 单次调用）。"""
    assert _registry() == {}

    batch = await _expect_no_loop_error(
        asyncio.to_thread(embedding.encode, ["hello"]), "to_thread sync encode"
    )
    assert batch.shape == (1, DIM)

    assert _registry() == {}, (
        "同步路径借用了 async client 分区表里的 httpx.AsyncClient —— 它是 loop-bound 的，"
        "必须只由 aencode() 在当前运行 loop 上创建"
    )


def test_aencode_under_two_short_lived_loops_uses_two_distinct_clients(
    embedding: ApiEmbedding,
) -> None:
    """两个先后销毁的 loop 各自必须拿到自己的 client，绝不复用上一个 loop 的。"""

    def _one_call() -> None:
        vector = asyncio.run(embedding.aencode("hello"))
        assert vector.shape == (DIM,)

    _one_call()
    first_client = _only_client()
    first_loop = _only_owner()
    assert first_loop is not None
    assert first_loop.is_closed(), (
        "第一次 asyncio.run 结束后归属 loop 必须已销毁，否则下面「不得把已销毁 "
        "loop 的 client 交给下一个 loop」这条断言就没有意义"
    )

    _one_call()
    second_client = _only_client()
    second_loop = _only_owner()
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
    assert _only_owner() is asyncio.get_running_loop()
    assert _registry()[asyncio.get_running_loop()] is not None


async def test_close_async_client_is_idempotent(embedding: ApiEmbedding) -> None:
    await embedding.aencode("hello")
    first = await close_async_client()
    second = await close_async_client()
    assert first.closed
    assert not second.closed, "重复关闭不得把「没东西可关」谎报成已关闭"
    assert _registry() == {}


def test_close_async_client_from_a_foreign_loop_does_not_raise(
    embedding: ApiEmbedding,
) -> None:
    """归属 loop 已销毁后，shutdown 路径不得再抛 Event loop is closed。

    `aclose()` 会顺着连接池去关 socket，而 socket 的 transport 记着自己创建
    时的那个 loop；对已销毁的 loop 调 `aclose()` 会在 loop 内部抛
    `RuntimeError: Event loop is closed`，把应用关闭 / 测试清理变成崩溃。
    """
    asyncio.run(embedding.aencode("hello"))  # client 归属这条已销毁的 loop
    dead_owner = _only_owner()
    assert dead_owner is not None
    assert dead_owner.is_closed()

    report = asyncio.run(close_async_client())  # 在另一条 loop 上执行关闭
    assert not report.closed, "不存在归属当前 loop 的 client 可关"
    assert report.dropped_dead_owners == 1, "已销毁归属的条目只能丢弃引用，不算关闭"
    assert _registry() == {}


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


# ── PR #54 review：每个存活 event loop 各自保留一个 client（per-loop partition）──
#
# 第一版修复只有一对模块级全局（``_async_client`` / ``_async_client_loop``）：
# 「归属 loop ≠ 当前 loop 就重建」。这治住了跨 loop 复用，却把 last-loop-wins
# 带进来了 —— 两条同时存活的 loop 交替调用时，每次调用都把对方**仍在运行**的
# client 顶掉，被顶掉的 client 既没关闭也没人再引用，连接池资源与 keep-alive 连接
# 一起泄漏，连接池复用也等于没了。下面这些用例把「同时存活的多条 loop」当成
# 一等公民来断言。


def _count_client_creations(
    monkeypatch: pytest.MonkeyPatch,
) -> list[httpx.AsyncClient]:
    """统计 ``_new_async_client()`` 的真实调用次数（= 实际建了多少个 client）。"""
    created: list[httpx.AsyncClient] = []
    real_new = api_embedding._new_async_client

    def _counting_new() -> httpx.AsyncClient:
        client = real_new()
        created.append(client)
        return client

    monkeypatch.setattr(api_embedding, "_new_async_client", _counting_new)
    return created


def _assert_no_thread_failure(failures: dict[str, BaseException]) -> None:
    """线程内的异常回主线程断言：任何 loop 生命周期错误都必须让用例失败。"""
    for tag, exc in failures.items():
        _fail_if_loop_lifecycle_error(exc)
        raise AssertionError(f"loop {tag} 期望成功，但抛出了 {type(exc).__name__}: {exc}")


def _run_two_live_loops_in_threads(
    embedding: ApiEmbedding,
    rounds: int,
    created: list[httpx.AsyncClient],
    body: Any = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """两条真实线程 + 两条**长期存活**的 event loop，按 A/B/A/B 轮次交替调用。

    每轮协议：两条 worker 各跑一次 ``body`` → 汇合到 barrier → 主线程**趁两条 loop
    仍然存活**读取分区表快照 → 放行下一轮。主线程参与 barrier 是为了把「两条 loop
    同时存活」这个前提变成同步事实，而不是靠 sleep 赌时序。

    返回 ``observed`` / ``created`` / ``registry_sizes`` / ``snapshots`` /
    ``failures``（线程内异常）。
    """
    barrier = threading.Barrier(3)
    observed: dict[str, list[Any]] = {"A": [], "B": []}
    registry_sizes: list[int] = []
    snapshots: list[dict[int, int]] = []
    failures: dict[str, BaseException] = {}

    async def _default_body(tag: str, index: int) -> Any:
        await embedding.aencode(f"{tag}-第{index}轮")
        return dict(async_client_registry_snapshot()).get(asyncio.get_running_loop())

    runner = body or _default_body

    def _worker(tag: str) -> None:
        loop = asyncio.new_event_loop()
        try:

            async def _main() -> None:
                for index in range(rounds):
                    observed[tag].append(await runner(tag, index))
                    barrier.wait(timeout=timeout)
                    barrier.wait(timeout=timeout)

            loop.run_until_complete(_main())
        except BaseException as exc:  # noqa: BLE001 - 带回主线程做断言
            failures[tag] = exc
            barrier.abort()
        finally:
            loop.close()

    threads = [
        threading.Thread(target=_worker, args=(tag,), name=f"embedding-loop-{tag}")
        for tag in ("A", "B")
    ]
    for thread in threads:
        thread.start()
    try:
        for _ in range(rounds):
            barrier.wait(timeout=timeout)
            snapshot = async_client_registry_snapshot()
            registry_sizes.append(len(snapshot))
            snapshots.append({id(owner): id(client) for owner, client in snapshot})
            barrier.wait(timeout=timeout)
    except threading.BrokenBarrierError:
        pass
    for thread in threads:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), "worker 线程没有退出，A/B 交替协议被卡死了"

    return {
        "observed": observed,
        "created": created,
        "registry_sizes": registry_sizes,
        "snapshots": snapshots,
        "failures": failures,
    }


def _run_harness(
    embedding: ApiEmbedding,
    monkeypatch: pytest.MonkeyPatch,
    rounds: int,
    body: Any = None,
) -> dict[str, Any]:
    """跑 A/B 交替协议，并同时统计真实创建的 client 个数。"""
    created = _count_client_creations(monkeypatch)
    return _run_two_live_loops_in_threads(embedding, rounds=rounds, created=created, body=body)


def test_two_live_loops_alternating_keep_one_client_each(
    embedding: ApiEmbedding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A/B/A/B 多轮交替：每条 loop 的 client identity 稳定，client 数量不增长。"""
    rounds = 4
    run = _run_harness(embedding, monkeypatch, rounds)
    _assert_no_thread_failure(run["failures"])

    first: dict[str, httpx.AsyncClient] = {}
    for tag in ("A", "B"):
        clients = run["observed"][tag]
        assert len(clients) == rounds, f"{tag} 只完成了 {len(clients)}/{rounds} 轮"
        assert all(c is not None for c in clients), (
            f"{tag} 的 loop 在分区表里查不到自己的 client —— 分区键不是归属 loop"
        )
        assert len({id(c) for c in clients}) == 1, (
            f"{tag} 的 client identity 在 {rounds} 轮交替中不稳定："
            f"{[id(c) for c in clients]}。另一条存活 loop 的调用把它顶掉了（last-loop-wins）"
        )
        first[tag] = clients[0]

    assert first["A"] is not first["B"], "两条 loop 共用了同一个 loop-bound client"
    assert len(run["created"]) == 2, (
        f"{rounds} 轮交替本应只建 2 个 client（每条存活 loop 一个），"
        f"实际建了 {len(run['created'])} 个"
    )
    assert set(run["registry_sizes"]) == {2}, (
        f"两条 loop 同时存活时分区表必须恰好 2 条，实际每轮规模为 {run['registry_sizes']}"
    )
    assert all(snap == run["snapshots"][0] for snap in run["snapshots"]), (
        f"分区表的 (loop, client) 配对在交替过程中发生了变化：{run['snapshots']}"
    )


def test_two_live_loops_never_share_a_connection_pool(
    embedding: ApiEmbedding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两条存活 loop 的连接池各自独立：A 的池绝不被 B 的请求使用。"""
    pools: dict[str, list[int]] = {"A": [], "B": []}

    async def _body(tag: str, index: int) -> None:
        await embedding.aencode(f"{tag}-{index}")
        client = await api_embedding._get_async_client()
        pools[tag].append(id(client._transport._pool))

    run = _run_harness(embedding, monkeypatch, rounds=3, body=_body)
    _assert_no_thread_failure(run["failures"])

    assert len(set(pools["A"])) == 1, f"A 的连接池被重建了：{pools['A']}"
    assert len(set(pools["B"])) == 1, f"B 的连接池被重建了：{pools['B']}"
    assert pools["A"][0] != pools["B"][0], "两条 loop 共享了同一个连接池"
    assert len(run["created"]) == 2


async def _one_shot_loop_call(awaitable: Any, timeout: float = 30.0) -> Any:
    """在一条全新的一次性 loop（独立线程，与本用例的主 loop 同时不存活）上 await 一次。"""
    return await asyncio.wait_for(asyncio.to_thread(asyncio.run, awaitable), timeout)


async def test_closed_loop_client_is_dropped_and_never_reused(
    embedding: ApiEmbedding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已销毁 loop 的 client 既不交给别的 loop，也不再在分区表里占位。"""
    created = _count_client_creations(monkeypatch)

    await embedding.aencode("主 loop")
    main_client = await api_embedding._get_async_client()

    await _one_shot_loop_call(embedding.aencode("第一条一次性 loop"))
    before = dict(async_client_registry_snapshot())
    assert len(before) == 2, "两条 loop 存活期间分区表必须各有 1 条"
    dead_owner, dead_client = next(
        (owner, client) for owner, client in before.items() if client is not main_client
    )
    assert dead_owner.is_closed(), "asyncio.run 结束后它的 loop 必须已销毁"

    captured: list[httpx.AsyncClient] = []

    async def _second_one_shot() -> None:
        await embedding.aencode("第二条一次性 loop")
        captured.append(await api_embedding._get_async_client())

    await _one_shot_loop_call(_second_one_shot())

    entries = dict(async_client_registry_snapshot())
    assert dead_owner not in entries, (
        "已销毁 loop 的 client 还留在分区表里 —— 条目不会随调用次数回收"
    )
    assert len(entries) == 2, f"期望主 loop + 新的一次性 loop 两条，实际 {len(entries)} 条"
    assert entries[asyncio.get_running_loop()] is main_client, (
        "别的 loop 的调用把主 loop 的 client 顶掉了"
    )
    assert captured[0] is not dead_client, "已销毁 loop 的 client 被交给了一条活着的 loop"
    assert captured[0] is not main_client
    assert len(created) == 3


def test_repeated_short_lived_loops_do_not_grow_the_registry(
    embedding: ApiEmbedding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续一次性 loop（asyncio.run 形态）不会让分区表无界增长。"""
    created = _count_client_creations(monkeypatch)

    for i in range(6):
        vector = asyncio.run(embedding.aencode(f"一次性-{i}"))
        assert vector.shape == (DIM,)
        size = len(async_client_registry_snapshot())
        assert size <= 1, f"第 {i} 轮后分区表里堆了 {size} 条 —— 上一个已销毁 loop 的条目没有被回收"

    assert len(created) == 6, "每个一次性 loop 建一个 client 是预期，但条目必须及时回收"
    assert len(async_client_registry_snapshot()) == 1


async def test_concurrent_tasks_on_one_loop_share_one_client(
    embedding: ApiEmbedding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一条 loop 上的并发任务只建一个 client（连接池复用没被并发首调打散）。"""
    created = _count_client_creations(monkeypatch)

    results = await asyncio.gather(*(embedding.aencode(f"q{i}") for i in range(8)))
    assert len(created) == 1, f"8 个并发任务建了 {len(created)} 个 client"
    for vector in results:
        assert vector.shape == (DIM,)
    assert dict(async_client_registry_snapshot()).keys() == {asyncio.get_running_loop()}


def test_shutdown_closes_only_the_calling_loop_and_reports_the_rest(
    embedding: ApiEmbedding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """shutdown 契约：只关自己 loop 的 client，别的存活 loop 既不关也不摘。"""
    reports: dict[str, Any] = {}
    b_clients: list[httpx.AsyncClient] = []

    async def _body(tag: str, index: int) -> None:
        await embedding.aencode(f"{tag}-{index}")
        if tag == "A" and index == 1:
            reports["A"] = await close_async_client()
        if tag == "B":
            b_clients.append(await api_embedding._get_async_client())

    run = _run_harness(embedding, monkeypatch, rounds=2, body=_body)
    _assert_no_thread_failure(run["failures"])

    report = reports["A"]
    assert report.closed, "A 关闭自己 loop 的 client 时必须真的 aclose 了"
    assert report.foreign_live_owners == 1, (
        f"B 的 client 归属另一条存活 loop，必须被如实计数，实际 {report.foreign_live_owners}"
    )
    assert report.dropped_dead_owners == 0

    assert len(b_clients) == 2
    assert b_clients[0] is b_clients[1], (
        "A 的 shutdown 把 B 的 client 摘掉了 —— 那会逼 B 重建连接池"
    )
    assert not b_clients[0].is_closed, "A 的 shutdown 跨 loop 关掉了 B 的 client"
    assert list(dict(async_client_registry_snapshot()).values()) == [b_clients[0]], (
        "A 的 shutdown 之后分区表必须只剩 B 那一条"
    )


def test_two_live_loops_keep_provider_errors_as_http_status_error(
    provider: _StubProvider, embedding: ApiEmbedding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两条存活 loop 交替之后，provider 错误类型不变（不是 loop 生命周期错误）。"""
    kinds: dict[str, str] = {}

    async def _body(tag: str, index: int) -> None:
        if index == 0:
            await embedding.aencode(f"{tag}-warmup")
            return
        provider.set_status(500)
        try:
            await embedding.aencode(f"{tag}-provider-error")
        except Exception as exc:  # noqa: BLE001 - 要断言异常种类
            _fail_if_loop_lifecycle_error(exc)
            kinds[tag] = type(exc).__name__
        else:
            kinds[tag] = "NO_ERROR"

    run = _run_harness(embedding, monkeypatch, rounds=2, body=_body)
    _assert_no_thread_failure(run["failures"])
    assert kinds == {"A": "HTTPStatusError", "B": "HTTPStatusError"}


def test_registry_lock_is_never_held_across_an_await() -> None:
    """静态契约：``threading.Lock`` 的临界区内不得出现 await。

    临界区只做纯同步的 dict 操作；``aclose()`` / ``post()`` 都在锁外 await。
    """
    for func in (
        api_embedding._get_async_client,
        api_embedding.close_async_client,
        api_embedding._drop_dead_owners_locked,
    ):
        block_indent: int | None = None
        for line in inspect.getsource(func).splitlines():
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if block_indent is not None:
                if stripped and indent <= block_indent:
                    block_indent = None
                elif "await " in stripped:
                    raise AssertionError(
                        f"{func.__name__} 在持有 threading.Lock 时 await：{stripped}"
                    )
            if stripped.startswith("with _registry_lock"):
                block_indent = indent
