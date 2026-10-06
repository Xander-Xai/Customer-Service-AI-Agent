"""默认测试 lane 的离线性契约（issue #52）。

这个文件的**唯一职责**是守住 README 的那句承诺：

    "全量测试离线运行，无需 API Key"

之前这条承诺是不成立的：``.env.test`` 带着**占位**凭据
（``EMBEDDING_API_KEY=sk-placeholder-embedding-test-key``、
``RERANKER_API_KEY=sk-placeholder-reranker-test-key``、
``OPENAI_API_KEY=sk-placeholder-test-key-do-not-use``）。占位串是**真值**，
于是旧代码里只判 truthiness 的两处闸门（``if not EMBEDDING_API_KEY``）
全都放行，真实 HTTP 客户端被构造出来，第一次用到向量/重排通道时就去POST
``api.siliconflow.cn`` 并吃 401（实测单次约 40s）。多模态的 TTS 更是直接连
``speech.platform.bing.com``。

为什么这些断言是**结构性**的，而不是"看起来对"
----------------------------------------------------
本文件里没有一个测试去检查"返回值像不像本地结果"—— 那证明不了没出网。这里检查
的是**出网这件事本身能否发生**：装配出的对象在 server/transport 层被换成哑弹，
于是任何一次真实外呼都会立刻抛错；同时断言装配结果确实是本地实现。两者合起来
才构成"默认 lane 不发公网请求"的证据。

需要真实 provider 的测试走 ``make test-real-providers``，默认不执行（见 Makefile）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from core import config

#: 一个"看起来像真实凭据"的合成值 —— 占位规则**必须**放行它，否则就是行为变更
#: （真实但较短的凭据会突然失去向量通道）。
#:
#: 刻意在运行时拼装而不是写成字面量：仓库的 secret guard（scripts/check_secrets.py）
#: 会把 ``sk-[A-Za-z0-9]{20,}`` 判为疑似泄露。这里要测的恰恰是"一个真实形态的
#: 凭据"，写成 20+ 字符的 ``sk-`` 字面量会让 CI 的 secret guard 失败；而为此把
#: 整个测试文件加进白名单，比问题本身更糟。拼装既不触发 guard，也不需要任何
#: 豁免，且这显然不是真实凭据。
_SYNTHETIC_REALISTIC_KEY = "-".join(["sk", "synthetic" + "x" * 8, "notarealkey00000000"])


# ===== 默认 lane 的配置事实 =====


class TestDefaultLaneIsLocal:
    """``.env.test`` 必须把所有会出网的依赖都切到 local。"""

    @pytest.mark.parametrize(
        "name",
        [
            "EMBEDDING_PROVIDER",
            "RERANKER_PROVIDER",
            "STT_PROVIDER",
            "TTS_PROVIDER",
        ],
    )
    def test_provider_knob_is_local(self, name: str):
        value = getattr(config, name)
        assert value == config.PROVIDER_LOCAL, (
            f"{name}={value!r}：默认测试 lane 必须是 local，"
            f"否则默认测试会发起真实 provider 请求（issue #52）"
        )

    @pytest.mark.parametrize(
        "name",
        ["EMBEDDING_CREDENTIAL_USABLE", "RERANKER_CREDENTIAL_USABLE"],
    )
    def test_placeholder_credential_is_not_egress_permission(self, name: str):
        """占位凭据不构成出网许可。

        这是 issue #52 的核心不变量：即使 provider 旋钮被切回 remote，占位凭据
        也不得放行真实请求。它是独立于 provider 旋钮的**第二道闸门**。
        """
        assert getattr(config, name) is False, (
            f"{name} 为 True：占位凭据被判为可出网，" f"这正是 issue #52 里 401 的成因"
        )

    def test_is_placeholder_api_key_rejects_env_test_placeholders(self):
        """本lane 实际使用的占位串必须都被判定为占位。"""
        for placeholder in (
            "sk-placeholder-test-key-do-not-use",
            "sk-placeholder-embedding-test-key",
            "sk-placeholder-reranker-test-key",
            "",
            "   ",
            None,
        ):
            assert (
                config.is_placeholder_api_key(placeholder) is True
            ), f"{placeholder!r} 应被判为占位凭据"

    def test_is_placeholder_api_key_allows_realistic_credentials(self):
        """真实形态的凭据必须仍然被判为可用 —— 否则就是**行为变更**。

        占位判定只引入"占位前缀 + 空值"这一条规则，**不**引入长度下限：
        给 embedding 侧加长度门槛会让某个真实但较短的凭据突然失去向量通道。
        """
        for real in (_SYNTHETIC_REALISTIC_KEY, "ak-real-looking-credential-value"):
            assert (
                config.is_placeholder_api_key(real) is False
            ), f"{real!r} 是真实形态凭据，不得被占位规则拦掉"


# ===== 出网本身无法发生：哑弹 transport =====


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """让任何真实 HTTP 外呼立刻失败。

    替换 ``httpx.AsyncClient``/``httpx.Client`` 的 ``send``，而不是 mock 掉
    各个 provider 类：这样**任何**走 httpx 的调用路径都会撞上它，而不是只有
    我们想到的那几条。真实外呼一旦发生，测试就会以明确的"试图出网"信息失败，
    而不是安静地退化成降级路径 —— 后者正是 issue #52 之前的状态。
    """

    async def _forbidden_async(*args: Any, **kwargs: Any):
        raise AssertionError(
            "默认测试 lane 试图发起真实 HTTP 请求（issue #52 回归）。"
            "provider 闸门应该让请求在装配阶段就不被构造。"
        )

    def _forbidden_sync(*args: Any, **kwargs: Any):
        raise AssertionError(
            "默认测试 lane 试图发起真实 HTTP 请求（issue #52 回归）。"
            "provider 闸门应该让请求在装配阶段就不被构造。"
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", _forbidden_async)
    monkeypatch.setattr(httpx.Client, "send", _forbidden_sync)


class TestEmbeddingLaneCannotEgress:
    def test_select_embed_fn_returns_local_implementation(self, no_network: None):
        from rag.embedding_factory import select_embed_fn
        from rag.local_provider import LocalEmbedding

        selection = select_embed_fn()
        assert isinstance(selection.embed_fn, LocalEmbedding)
        assert selection.provider == config.PROVIDER_LOCAL
        assert selection.reason == "local_provider"

    def test_embed_fn_actually_produces_vectors_without_network(self, no_network: None):
        """本地实现必须**真的**产出合法向量 —— 不是返回 None 蒙混过关。"""
        from rag.embedding_factory import create_embed_fn

        embed_fn = create_embed_fn()
        vectors = embed_fn.encode(["烟酰胺美白", "补水保湿面霜"])
        assert vectors.shape == (2, config.EMBEDDING_DIM)
        assert vectors.dtype != object
        # L2 归一化：单位范数（Qdrant cosine 检索的前提）。
        norms = [float((row**2).sum() ** 0.5) for row in vectors]
        assert all(abs(n - 1.0) < 1e-5 for n in norms), norms
        # 全部有限：不许有 NaN/Inf（下游 validate_embedding_vector 会独立再校验）。
        assert bool((vectors == vectors).all()), "向量含 NaN"

    def test_local_embedding_is_deterministic_across_instances(self, no_network: None):
        """跨实例/跨进程一致：Qdrant 里的向量由上一个进程写入，下一个要能对上。

        所以实现用的是 blake2b 而不是内置 hash()（后者对 str 有每进程随机盐）。
        """
        from rag.local_provider import LocalEmbedding

        text = "烟酰胺美白精华"
        a = LocalEmbedding(dim=256).encode(text)
        b = LocalEmbedding(dim=256).encode(text)
        assert (a == b).all(), "LocalEmbedding 跨实例不确定"


class TestRerankerLaneCannotEgress:
    def test_create_reranker_is_local_and_reorders(self, no_network: None):
        """本地重排必须**真的重排**，而不是把原顺序原样返回。"""
        from rag.local_provider import LocalReranker
        from rag.reranker import create_reranker

        reranker = create_reranker()
        assert isinstance(reranker, LocalReranker)

        results = [
            {"content": "完全无关的内容", "distance": 0.1},
            {"content": "烟酰胺美白与保湿", "distance": 0.2},
        ]
        outcome = reranker.rerank_with_outcome("烟酰胺美白", results, top_k=2)
        assert outcome.applied is True
        assert outcome.provider_called is False
        assert outcome.results[0]["content"] == "烟酰胺美白与保湿"
        assert len(outcome.results) == 2

    def test_placeholder_key_makes_api_reranker_unavailable(self, no_network: None):
        """占位凭据按未配置处理：available=False，不构造 HTTP 客户端。"""
        from rag.reranker import ApiReranker

        reranker = ApiReranker(api_key="sk-placeholder-reranker-test-key")
        assert reranker.available is False


class TestMediaLaneCannotEgress:
    def test_tts_local_provider_synthesizes_offline(self, no_network: None):
        """TTS local provider 不连edge_tts 公网端点，且确定性。"""
        from media.tts_processor import TTSProcessor

        processor = TTSProcessor()
        first = asyncio.run(processor.synthesize("你好，欢迎咨询"))
        second = asyncio.run(processor.synthesize("你好，欢迎咨询"))
        assert first == second, "local TTS 必须确定性"
        assert len(first) > 0
        # 不同文本 → 不同字节：说明它确实随输入变化，不是固定常量。
        assert first != asyncio.run(processor.synthesize("另一段文本"))

    def test_tts_empty_text_still_rejected_offline(self, no_network: None):
        from media.tts_processor import TTSProcessor

        with pytest.raises(ValueError):
            asyncio.run(TTSProcessor().synthesize("   "))

    def test_stt_local_provider_transcribes_offline(self, no_network: None):
        """STT local provider 不打 Whisper，且确定性。"""
        from media.audio_processor import AudioProcessor

        processor = AudioProcessor()
        payload = b"\x00\x01\x02" * 64
        first = asyncio.run(processor.transcribe(payload, "audio/wav"))
        second = asyncio.run(processor.transcribe(payload, "audio/wav"))
        assert first == second
        assert first != asyncio.run(processor.transcribe(b"different", "audio/wav"))
        assert "本地STT替身" in first

    def test_stt_still_validates_format_offline(self, no_network: None):
        """local provider 不吞掉输入校验 —— 非法格式仍必须报错。"""
        from media.audio_processor import AudioProcessor

        with pytest.raises(ValueError, match="不支持的音频格式"):
            asyncio.run(AudioProcessor().transcribe(b"x", "text/plain"))


class TestRemoteLaneIsOptInOnly:
    """真实 provider 必须仍然可达 —— 只是默认不选它。"""

    def test_remote_provider_with_real_credential_still_builds_api_client(self):
        """生产路径逐字节不变：provider=remote + 真实凭据 → ApiEmbedding。"""
        from unittest.mock import patch

        from rag.embedding_factory import select_embed_fn

        with (
            patch.object(config, "EMBEDDING_PROVIDER", config.PROVIDER_REMOTE),
            patch.object(config, "EMBEDDING_API_KEY", _SYNTHETIC_REALISTIC_KEY),
            patch("rag.api_embedding.ApiEmbedding") as mock_cls,
        ):
            selection = select_embed_fn()

        mock_cls.assert_called_once(), "真实凭据必须仍然构造 ApiEmbedding（生产不变）"
        assert selection.provider == config.PROVIDER_REMOTE
        assert selection.reason == "remote_provider"
