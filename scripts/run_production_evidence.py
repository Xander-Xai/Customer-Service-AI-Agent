#!/usr/bin/env python3
"""Run local evidence contracts; external suites are fail-closed and disabled."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.runner import _unverified_record, run_local, write_run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("local", "provider", "redis", "erp", "qdrant"), default="local")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("artifacts/evidence/local.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("artifacts/evidence/local.md"))
    args = parser.parse_args()

    if args.suite == "local":
        payload = run_local(repeat=args.repeat, warmup=args.warmup).to_dict()
    else:
        payload = _unverified_record(args.suite)
    write_run(payload, args.output, args.markdown_output)
    print(json.dumps({"status": payload["environment"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
