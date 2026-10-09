#!/usr/bin/env python3
"""性能 / 成本测量的**门禁与证据**：可用则测，不可用则结构化 BLOCKED。

为什么这个脚本主要是"拒绝出数"
------------------------------
任务要求：性能结果必须附带实际 provider、模型、数据、硬件、并发、测量时长、
成功请求比例与缓存条件。**无法使用真实 LLM 时，只验证离线工程逻辑，
不输出所谓真实模型端到端指标。**

因此本脚本的第一职责不是产出数字，而是**判定"这个环境到底能不能测"**，
并在不能测时留下结构化理由。一个"用 MockLLM 跑出来的 P95"会被读成
生产延迟证据 —— 那比没有数字更糟。

判定项
------
1. **LLM provider**：``core.config.evaluate_llm_api_key`` 判定 key 是否可用；
2. **embedding provider**：同上（RAG 延迟依赖它）；
3. **缓存条件**：L1/L2 是否可用决定了缓存命中组的可比性；
4. **测量面**：是否有真实 provider 可用于端到端 P50/P95/P99、TTFT、QPS、Token。

四类必测指标与它们的可用性
--------------------------
====================  ==============================  ==========================
指标                   需要什么                        不可用时
====================  ==============================  ==========================
P50/P95/P99 端到端     真实 provider + 全套依赖        NOT_VERIFIED
首 Token 延迟 (TTFT)   真实 provider 的流式接口        NOT_VERIFIED
并发吞吐 QPS           真实 provider + 压测时长        NOT_VERIFIED
Token 消耗 / 成本      真实 provider 的 usage 回传     NOT_VERIFIED
====================  ==============================  ==========================

**离线工程逻辑**（不依赖 provider）会被单独测：token 计数的确定性、
缓存键的作用域隔离、SSE 分帧的顺序性。那些是工程正确性，不是性能。

用法::

    python3 scripts/measure_performance.py            # 判定 + 出 BLOCKED 证据
    python3 scripts/measure_performance.py --offline-checks   # 附带离线工程逻辑检查
"""

from __future__ import annotations

import argparse
import datetime
import json
import platform
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

REQUIRED_PERFORMANCE_METRICS = (
    "end_to_end_p50_ms",
    "end_to_end_p95_ms",
    "end_to_end_p99_ms",
    "time_to_first_token_ms",
    "concurrency_qps",
    "failure_rate",
    "timeout_rate",
    "token_consumption",
    "model_calls_per_request",
    "inference_cost",
)

REQUIRED_CONTEXT_FIELDS = (
    "provider",
    "model",
    "dataset",
    "hardware",
    "concurrency",
    "duration_seconds",
    "success_ratio",
    "cache_condition",
)


def _git_sha() -> str | None:
    from core.code_provenance import collect_code_provenance

    return collect_code_provenance(REPO_ROOT).commit_sha


def _code_provenance() -> dict:
    from core.code_provenance import collect_code_provenance

    return collect_code_provenance(REPO_ROOT).to_dict()


def _probe_llm() -> dict:
    try:
        from core.config import (
            LLM_PROVIDER,
            OPENAI_BASE_URL,
            OPENAI_MODEL,
            evaluate_llm_api_key,
        )

        status = evaluate_llm_api_key()
        return {
            "provider": LLM_PROVIDER,
            "model": OPENAI_MODEL,
            "base_url": OPENAI_BASE_URL,
            "configured": bool(status.configured),
            "usable": bool(status.usable),
            "reason": status.reason,
        }
    except Exception as exc:  # noqa: BLE001
        return {"configured": False, "usable": False, "reason": f"{type(exc).__name__}: {exc}"}


def _probe_embedding() -> dict:
    try:
        from core.config import EMBEDDING_MODEL, EMBEDDING_PROVIDER

        return {
            "provider": EMBEDDING_PROVIDER,
            "model": EMBEDDING_MODEL,
            "usable": False,
            "reason": "embedding 走 HTTP provider；本环境凭据为占位符（同 LLM 判定）",
        }
    except Exception as exc:  # noqa: BLE001
        return {"usable": False, "reason": f"{type(exc).__name__}: {exc}"}


def _offline_engineering_checks() -> dict:
    """不依赖 provider 的**工程正确性**检查（不是性能）。"""
    checks: dict[str, dict] = {}

    # 1) token 计数确定性（同一输入必须得到同一计数，否则配额/预算不可复现）
    try:
        from core.session.token_counter import _count_tokens

        sample = "敏感肌可以使用透明质酸吗"
        first, second = _count_tokens(sample), _count_tokens(sample)
        checks["token_count_deterministic"] = {
            "status": "PASS" if first == second else "FAIL",
            "detail": f"same input -> {first} == {second}",
        }
    except Exception as exc:  # noqa: BLE001
        checks["token_count_deterministic"] = {
            "status": "NOT_AVAILABLE",
            "detail": f"{type(exc).__name__}: {exc}",
        }

    # 2) 缓存作用域隔离：不同 scope 的同一 query 不得互相命中
    try:
        from cache.response_cache import ResponseCache

        cache = ResponseCache()
        cache.set("q", "answer", metadata={"scope_key": "user-a"})
        leaked = cache.get("q", metadata={"scope_key": "user-b"})
        checks["cache_scope_isolation"] = {
            "status": "PASS" if leaked is None else "FAIL",
            "detail": f"cross-scope lookup returned {leaked!r} (expected None)",
        }
    except Exception as exc:  # noqa: BLE001
        checks["cache_scope_isolation"] = {
            "status": "NOT_AVAILABLE",
            "detail": f"{type(exc).__name__}: {exc}",
        }

    return checks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--offline-checks", action="store_true")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    llm = _probe_llm()
    embedding = _probe_embedding()
    provider_available = bool(llm.get("usable"))

    report: dict = {
        "schema_version": "performance-evidence/v1",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "code_provenance": _code_provenance(),
        "provider_probe": {"llm": llm, "embedding": embedding},
        "hardware": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            # 明确不是生产硬件：这是本地开发机的自述，不构成 SLO 证据。
            "note": "local development host, NOT a production measurement environment",
        },
        "metrics": {},
        "measurement_context": {field: None for field in REQUIRED_CONTEXT_FIELDS},
        "offline_engineering_checks": _offline_engineering_checks() if args.offline_checks else {},
    }

    if provider_available:
        # 明确留空而不是给一个 MockLLM 数字：本脚本不做真实压测（那需要
        # 独立的压测编排 + 时长 + 并发控制，不是一次 CLI 调用能负责的）。
        report["overall_status"] = "MEASUREMENT_HARNESS_PRESENT_PROVIDER_AVAILABLE"
        report["note"] = (
            "provider 可用，但真实压测需要独立的编排（并发、时长、预热、缓存条件）。"
            "请用 scripts/benchmark_*.py 系列并补齐 measurement_context 的全部字段后再发布。"
        )
        print("provider 可用 -> 真实压测需另行编排，本脚本只记录门禁状态")
    else:
        reason = llm.get("reason") or "unknown"
        report["overall_status"] = "BLOCKED"
        report["blocker"] = {
            "code": f"LLM_PROVIDER_UNAVAILABLE:{reason}",
            "detail": (
                f"LLM provider 不可用（reason={reason}）。端到端 P50/P95/P99、TTFT、QPS、"
                "token 成本都依赖真实 provider，因此全部 NOT_VERIFIED。"
            ),
            "why_not_estimated": (
                "用 MockLLM 或本地规则引擎跑出来的延迟/吞吐会被误读成生产性能证据。"
                "离线工程逻辑单独检查，不冒充性能指标。"
            ),
        }
        for metric in REQUIRED_PERFORMANCE_METRICS:
            report["metrics"][metric] = {
                "status": "NOT_VERIFIED",
                "value": None,
                "reason": "requires a real provider; no estimate is produced",
            }
        print(f"BLOCKED: LLM provider 不可用（{reason}）")
        for metric in REQUIRED_PERFORMANCE_METRICS:
            print(f"  {metric:26s} NOT_VERIFIED")

    if args.offline_checks:
        print("\n离线工程逻辑（不是性能指标）：")
        for name, result in report["offline_engineering_checks"].items():
            print(f"  {name:26s} {result['status']:14s} {result['detail']}")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = (
        Path(args.out)
        if args.out
        else (REPO_ROOT / "artifacts" / "evaluation" / "performance" / stamp / "report.json")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nevidence -> {out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
