"""Versioned, non-production workloads for harness validation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkloadCase:
    workload_id: str
    version: str
    scenario: str
    query: str
    expected_doc_ids: tuple[str, ...] = ()
    ranked_doc_ids: tuple[str, ...] = ()
    expected_fields: tuple[str, ...] = ()
    observed_fields: tuple[str, ...] = ()
    expected_tool: str | None = None
    observed_tool: str | None = None
    expected_degraded_signal: str | None = None
    observed_degraded_signal: str | None = None
    recoverability: str = "normal_success"


WORKLOAD_VERSION = "local-v1"


def local_workloads() -> tuple[WorkloadCase, ...]:
    return (
        WorkloadCase(
            "faq-001",
            WORKLOAD_VERSION,
            "simple_faq",
            "退货期限是什么？",
            ("faq-return",),
            ("faq-return", "faq-other"),
            ("return_window",),
            ("return_window",),
            recoverability="normal_success",
        ),
        WorkloadCase(
            "rag-001",
            WORKLOAD_VERSION,
            "rag_query",
            "如何处理发票问题？",
            ("billing-invoice",),
            ("billing-invoice", "billing-other"),
            ("invoice_process",),
            ("invoice_process",),
            recoverability="normal_success",
        ),
        WorkloadCase(
            "hybrid-001",
            WORKLOAD_VERSION,
            "hybrid_retrieval",
            "技术支持联系方式",
            ("tech-contact",),
            ("tech-contact", "tech-other"),
            recoverability="normal_success",
        ),
        WorkloadCase(
            "tool-001",
            WORKLOAD_VERSION,
            "tool_call",
            "查询订单状态",
            expected_fields=("order_status",),
            observed_fields=("order_status",),
            expected_tool="get_order_status",
            observed_tool="get_order_status",
        ),
        WorkloadCase(
            "multi-tool-001",
            WORKLOAD_VERSION,
            "multi_tool",
            "查询订单并计算退款",
            expected_fields=("order_status", "refund_amount"),
            observed_fields=("order_status", "refund_amount"),
            expected_tool="get_order_status+calculate_refund",
            observed_tool="get_order_status+calculate_refund",
        ),
        WorkloadCase(
            "degraded-embedding-001",
            WORKLOAD_VERSION,
            "degraded_embedding",
            "退货条件（向量不可用）",
            ("faq-return",),
            ("faq-return",),
            expected_degraded_signal="lexical_only",
            observed_degraded_signal="lexical_only",
            recoverability="degraded_success",
        ),
        WorkloadCase(
            "degraded-redis-001",
            WORKLOAD_VERSION,
            "degraded_redis",
            "缓存不可用时仍返回答案",
            ("faq-return",),
            ("faq-return",),
            expected_degraded_signal="cache_bypass",
            observed_degraded_signal="cache_bypass",
            recoverability="degraded_success",
        ),
        WorkloadCase(
            "erp-mock-001",
            WORKLOAD_VERSION,
            "erp_mock_staging",
            "查询测试订单分页结果",
            expected_fields=("page_info", "order_status"),
            observed_fields=("page_info", "order_status"),
            expected_tool="erp_list_orders",
            observed_tool="erp_list_orders",
        ),
        WorkloadCase(
            "degraded-reranker-001",
            WORKLOAD_VERSION,
            "degraded_reranker",
            "重排服务不可用时返回候选",
            ("faq-return",),
            ("faq-return",),
            expected_degraded_signal="rerank_skipped",
            observed_degraded_signal="rerank_skipped",
            recoverability="degraded_success",
        ),
        WorkloadCase(
            "degraded-qdrant-001",
            WORKLOAD_VERSION,
            "degraded_qdrant",
            "向量库不可用时明确失败",
            expected_fields=("degraded_status",),
            observed_fields=("degraded_status",),
            expected_degraded_signal="qdrant_unavailable",
            observed_degraded_signal="qdrant_unavailable",
            recoverability="graceful_failure",
        ),
        WorkloadCase(
            "provider-timeout-001",
            WORKLOAD_VERSION,
            "provider_timeout",
            "provider 超时时记录可恢复失败",
            expected_fields=("timeout_status",),
            observed_fields=("timeout_status",),
            expected_degraded_signal="provider_timeout",
            observed_degraded_signal="provider_timeout",
            recoverability="graceful_failure",
        ),
        WorkloadCase(
            "erp-timeout-001",
            WORKLOAD_VERSION,
            "erp_timeout",
            "ERP 超时时不伪造成功结果",
            expected_fields=("timeout_status",),
            observed_fields=("timeout_status",),
            expected_degraded_signal="erp_timeout",
            observed_degraded_signal="erp_timeout",
            recoverability="graceful_failure",
        ),
    )
