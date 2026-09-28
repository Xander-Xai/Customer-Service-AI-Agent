#!/usr/bin/env python3
"""Detect high-confidence private material without printing its contents.

The default mode scans files tracked by the current checkout. ``--history``
scans every unique historical blob reachable from the local repository. The
history mode is intentionally manual/scheduled: old findings are expected
until they are retired or the repository is migrated with an approved
history-rewrite plan.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_PEM_BEGIN = "-" * 5 + "BEGIN "
_PEM_END = "-" * 5
_PRIVATE_KEY_RE = re.compile(
    re.escape(_PEM_BEGIN) + r"(?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY" + re.escape(_PEM_END)
)
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")


@dataclass(frozen=True)
class Finding:
    location: str
    category: str


def _decode(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_text(location: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    patterns = (
        ("private-key-material", _PRIVATE_KEY_RE),
        ("openai-api-key", _OPENAI_KEY_RE),
        ("aws-access-key", _AWS_KEY_RE),
        ("github-token", _GITHUB_TOKEN_RE),
    )
    for category, pattern in patterns:
        if pattern.search(text):
            findings.append(Finding(location, category))
    return findings


def _run(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def tracked_paths() -> Iterable[Path]:
    output = _run("ls-files", "-z")
    for raw_path in output.split(b"\0"):
        if raw_path:
            yield Path(raw_path.decode("utf-8"))


def scan_current_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in tracked_paths():
        absolute = root / path
        if not absolute.is_file():
            continue
        text = _decode(absolute.read_bytes())
        if text is not None:
            findings.extend(scan_text(path.as_posix(), text))
    return findings


def scan_history() -> list[Finding]:
    findings: list[Finding] = []
    objects = _run("rev-list", "--objects", "--all").decode("utf-8", errors="replace")
    entries = [line.split(" ", 1) for line in objects.splitlines() if " " in line]
    if not entries:
        return findings
    process = subprocess.run(
        ["git", "cat-file", "--batch"],
        input=("\n".join(entry[0] for entry in entries) + "\n").encode("ascii"),
        capture_output=True,
        check=True,
    )
    stream = memoryview(process.stdout)
    offset = 0
    for object_id, path in entries:
        header_end = process.stdout.find(b"\n", offset)
        if header_end == -1:
            break
        header = process.stdout[offset:header_end].split()
        offset = header_end + 1
        if len(header) < 3:
            continue
        size = int(header[2])
        data = bytes(stream[offset : offset + size])
        offset += size + 1  # blob payload followed by the batch delimiter newline
        if header[1] != b"blob":
            continue
        text = _decode(data)
        if text is not None:
            findings.extend(scan_text(f"{object_id[:12]}:{path}", text))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        action="store_true",
        help="scan all unique historical blobs reachable from local refs",
    )
    args = parser.parse_args()

    root = Path.cwd()
    findings = scan_history() if args.history else scan_current_tree(root)
    if findings:
        print("Secret guard findings (content intentionally omitted):")
        for finding in findings:
            print(f"- {finding.location}: {finding.category}")
        return 1
    print("Secret guard passed: no high-confidence secret patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
