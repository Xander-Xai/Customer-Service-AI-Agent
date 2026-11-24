"""Read-only, format-only inventory of an authorized offline session export.

Input: one JSON-encoded stored string per line (not session IDs or Redis keys).
Output: aggregate counts only. No decryption, writes or provenance inference.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from core.session.session_manager import _SESSION_ENVELOPE_PREFIX, _is_fernet_token

MAX_LINE_BYTES = 4 * 1024 * 1024
KINDS = ("marked_envelope", "unmarked_token_candidate", "plaintext_json_candidate", "unrecognized")


def classify(stored: str) -> str:
    if stored.startswith(_SESSION_ENVELOPE_PREFIX):
        return "marked_envelope"
    if _is_fernet_token(stored):
        return "unmarked_token_candidate"
    try:
        json.loads(stored)
    except (ValueError, RecursionError):
        return "unrecognized"
    return "plaintext_json_candidate"


def inventory(path: Path) -> dict:
    counts = Counter(dict.fromkeys(KINDS, 0))
    with path.open("rb") as stream:
        while line := stream.readline(MAX_LINE_BYTES + 1):
            if len(line) > MAX_LINE_BYTES:
                raise ValueError("Export record exceeds inventory limit")
            try:
                stored = json.loads(line)
            except (ValueError, RecursionError):
                raise ValueError("Invalid export record") from None
            if not isinstance(stored, str):
                raise ValueError("Export must contain JSON strings only")
            counts[classify(stored)] += 1
    return {
        "schema": "session-format-inventory/v1",
        "total": sum(counts.values()),
        "counts": dict(counts),
        "ciphertext_authentication_verified": False,
        "plaintext_provenance_verified": False,
        "safe_to_flip_reject_mode": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = inventory(args.input)
    except (OSError, ValueError):
        parser.exit(2, "Session inventory failed; no record content emitted.\n")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
