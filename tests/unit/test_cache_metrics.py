"""缓存命中率统计单元测试。"""

import pytest

try:
    from core.monitoring import (
        MetricsCollector,
        cache_l1_hits_total,
        cache_l2_hits_total,
        cache_l3_hits_total,
        cache_fallback_total,
        stream_ttfb_seconds,
        trace_spans_total,
        rag_queries_total,
        rag_recall_at_3,
        scene_routing_total,
        active_components_total,
    )
    PROMETHEUS_AVAILABLE = True
except (ImportError, NameError):
    PROMETHEUS_AVAILABLE = False


@pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="Prometheus 未安装")
class TestCacheMetrics:
    """缓存相关 Prometheus 指标测试"""

    def test_cache_l1_metric_exists(self):
        """cache_l1_hits_total 指标存在"""
        assert cache_l1_hits_total is not None
        assert hasattr(cache_l1_hits_total, "inc")

    def test_cache_l2_metric_exists(self):
        """cache_l2_hits_total 指标存在"""
        assert cache_l2_hits_total is not None
        assert hasattr(cache_l2_hits_total, "inc")

    def test_cache_l3_metric_exists(self):
        """cache_l3_hits_total 指标存在"""
        assert cache_l3_hits_total is not None
        assert hasattr(cache_l3_hits_total, "inc")

    def test_cache_fallback_metric_exists(self):
        """cache_fallback_total 指标存在"""
        assert cache_fallback_total is not None
        assert hasattr(cache_fallback_total, "inc")

    def test_stream_ttfb_metric_exists(self):
        """stream_ttfb_seconds 指标存在"""
        assert stream_ttfb_seconds is not None
        assert hasattr(stream_ttfb_seconds, "observe")

    def test_trace_spans_metric_exists(self):
        """trace_spans_total 指标存在"""
        assert trace_spans_total is not None
        assert hasattr(trace_spans_total, "inc")

    def test_rag_queries_metric_exists(self):
        """rag_queries_total 指标存在"""
        assert rag_queries_total is not None
        assert hasattr(rag_queries_total, "inc")

    def test_rag_recall_metric_exists(self):
        """rag_recall_at_3 指标存在"""
        assert rag_recall_at_3 is not None
        assert hasattr(rag_recall_at_3, "set")

    def test_scene_routing_metric_exists(self):
        """scene_routing_total 指标存在"""
        assert scene_routing_total is not None
        assert hasattr(scene_routing_total, "labels")

    def test_active_components_metric_exists(self):
        """active_components_total 指标存在"""
        assert active_components_total is not None
        assert hasattr(active_components_total, "set")

    def test_metrics_can_be_incremented(self):
        """Prometheus 指标可以自增"""
        cache_l1_hits_total.inc()
        cache_l2_hits_total.inc()
        cache_l3_hits_total.inc()
        cache_fallback_total.inc()
        trace_spans_total.inc()
        rag_queries_total.inc()
        scene_routing_total.labels(scene_name="售前咨询").inc()


@pytest.mark.asyncio
class TestMetricsCollectorNewMethods:
    """MetricsCollector 新方法的测试"""

    async def test_record_cache_hit_l1(self):
        """record_cache_hit(1) 记录 L1 命中"""
        mc = MetricsCollector()
        mc.cache_hits = 0
        await mc.record_cache_hit(1)
        assert mc.cache_hits == 1

    async def test_record_cache_hit_l2(self):
        """record_cache_hit(2) 记录 L2 命中"""
        mc = MetricsCollector()
        mc.cache_hits = 0
        await mc.record_cache_hit(2)
        assert mc.cache_hits == 1

    async def test_record_cache_hit_l3(self):
        """record_cache_hit(3) 记录 L3 命中"""
        mc = MetricsCollector()
        mc.cache_hits = 0
        await mc.record_cache_hit(3)
        assert mc.cache_hits == 1

    async def test_record_stream_ttfb(self):
        """record_stream_ttfb 可以记录首字响应时间"""
        mc = MetricsCollector()
        await mc.record_stream_ttfb(0.5)  # 不抛出异常即可

    async def test_record_trace_span(self):
        """record_trace_span 可以记录 trace"""
        mc = MetricsCollector()
        await mc.record_trace_span()  # 不抛出异常即可

    async def test_record_rag_query(self):
        """record_rag_query 记录 RAG 查询"""
        mc = MetricsCollector()
        await mc.record_rag_query()
        # 不抛出异常即可

    async def test_record_rag_query_with_recall(self):
        """record_rag_query 支持记录召回率"""
        mc = MetricsCollector()
        await mc.record_rag_query(recall=0.95)

    async def test_record_scene_route(self):
        """record_scene_route 记录场景路由"""
        mc = MetricsCollector()
        await mc.record_scene_route("售前咨询")

    async def test_set_active_components(self):
        """set_active_components 设置组件数"""
        mc = MetricsCollector()
        await mc.set_active_components(15)


@pytest.mark.asyncio
class TestMetricsCollectorIntegration:
    """MetricsCollector 集成测试"""

    async def test_full_flow(self):
        """完整测试：记录请求 + 新指标方法"""
        mc = MetricsCollector()

        # 记录请求
        await mc.record_request(
            elapsed=0.5,
            agent="test_agent",
            mode="sequential",
            cached=False,
        )

        # 记录缓存命中
        await mc.record_cache_hit(1)
        await mc.record_cache_hit(2)
        await mc.record_cache_hit(3)

        # 记录 trace
        await mc.record_trace_span()

        # 记录 RAG
        await mc.record_rag_query(recall=0.95)

        # 记录场景
        await mc.record_scene_route("售前咨询")

        stats = await mc.get_stats()
        assert stats["total_requests"] >= 1

    async def test_cache_hits_reported_in_stats(self):
        """缓存命中次数反映在统计中"""
        mc = MetricsCollector()
        mc.cache_hits = 5
        await mc.record_cache_hit(1)
        assert mc.cache_hits == 6