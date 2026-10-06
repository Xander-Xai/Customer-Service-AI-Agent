#!/usr/bin/env python3
"""Run local evidence contracts; external suites are fail-closed and disabled."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.provider_runner import run_provider_staging
from evaluation.runner import _unverified_record, run_local, write_run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("local", "provider", "provider-staging", "redis", "erp", "qdrant"),
        default="local",
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--max-requests", type=int, default=20)
    parser.add_argument("--max-input-tokens", type=int, default=2000)
    parser.add_argument("--max-output-tokens", type=int, default=256)
    parser.add_argument("--estimated-cost-cap", type=float, default=1.0)
    parser.add_argument("--estimated-input-cost-per-1k", type=float, default=None)
    parser.add_argument("--estimated-output-cost-per-1k", type=float, default=None)
    parser.add_argument("--output", type=Path, default=Path("artifacts/evidence/local.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("artifacts/evidence/local.md"))
    args = parser.parse_args()

    if args.suite == "local":
        payload = run_local(repeat=args.repeat, warmup=args.warmup).to_dict()
    elif args.suite == "provider-staging":
        result = run_provider_staging(
            repeat=args.repeat,
            warmup=args.warmup,
            max_requests=args.max_requests,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            estimated_cost_cap=args.estimated_cost_cap,
            estimated_input_cost_per_1k=args.estimated_input_cost_per_1k,
            estimated_output_cost_per_1k=args.estimated_output_cost_per_1k,
        )
        payload = result.to_dict() if hasattr(result, "to_dict") else result
    else:
        payload = _unverified_record(args.suite)
    write_run(payload, args.output, args.markdown_output)
    print(
        json.dumps(
            {"status": payload["environment"], "output": str(args.output)}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
