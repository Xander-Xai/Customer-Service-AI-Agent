#!/usr/bin/env python3
"""Generate docs/openapi.json from the live FastAPI application.

Deterministic output:
- UTF-8, ensure_ascii=False
- indent=2, key order follows the app's insertion order (no sort_keys)
- trailing newline

Usage:
    python3 scripts/generate_openapi.py          # write docs/openapi.json
    python3 scripts/generate_openapi.py --check  # exit 1 if snapshot drifts
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = PROJECT_ROOT / "docs" / "openapi.json"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def build_spec() -> dict:
    """Import the app lazily so this module stays side-effect free on import."""
    from api.app_factory import app

    return app.openapi()


def serialize(spec: dict) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare with the existing snapshot and exit 1 on drift",
    )
    args = parser.parse_args()

    spec = build_spec()
    rendered = serialize(spec)

    if args.check:
        current = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else ""
        if not current.strip():
            print("DRIFT: docs/openapi.json missing")
            return 1
        current_spec = json.loads(current)
        # Surface comparison (path → method map): fastapi/pydantic are floating
        # in requirements.txt, so serialized schema content legitimately differs
        # across environments even for the same API. The portable contract is
        # the surface, not byte-identical JSON (same rule as
        # scripts/audit_doc_consistency.py::check_openapi_snapshot).
        current_surface = {
            p: sorted(o.keys()) for p, o in sorted(current_spec.get("paths", {}).items())
        }
        live_surface = {p: sorted(o.keys()) for p, o in sorted(spec["paths"].items())}
        if current_surface != live_surface:
            print(
                f"DRIFT: docs/openapi.json has {len(current_surface)} paths vs "
                f"app.openapi() {len(live_surface)} paths (or different path/method surface)"
            )
            return 1
        if current_spec.get("info", {}).get("version") != spec.get("info", {}).get("version"):
            print("DRIFT: docs/openapi.json info.version != app.openapi() info.version")
            return 1
        print(f"OK: docs/openapi.json surface matches app.openapi() ({len(spec['paths'])} paths)")
        return 0

    SNAPSHOT.write_text(rendered, encoding="utf-8")
    print(f"Wrote {SNAPSHOT.relative_to(PROJECT_ROOT)}: {len(spec['paths'])} HTTP paths")
    return 0


if __name__ == "__main__":
    sys.exit(main())
