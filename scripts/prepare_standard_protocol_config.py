"""Create first-boot local-only protocol credentials in a private Compose volume."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import secrets
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "deploy/fixtures/after-sales-procedures.synthetic.json"
PRODUCT_CATALOG = ROOT / "deploy/fixtures/product-support-documents.synthetic.json"
OUTPUT_NAME = "protocol.env"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    output_dir = args.output_directory
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    output = output_dir / OUTPUT_NAME
    mode = os.getenv("CSAI_PROTOCOL_MODE", "synthetic").lower()
    runtime_profile = os.getenv("CSAI_RUNTIME_PROFILE", "standard").lower()
    if mode not in {"synthetic", "external"}:
        raise ValueError("CSAI_PROTOCOL_MODE must be synthetic or external")
    if runtime_profile == "industrial" and mode != "external":
        raise RuntimeError("Industrial profile rejects synthetic protocol fixtures")
    app_user = pwd.getpwnam("appuser")
    if mode == "external":
        if output.exists():
            if output.read_text(encoding="ascii") == "":
                return 0
            raise RuntimeError(
                "Cannot switch a persisted synthetic credential volume to external mode"
            )
        output.touch(mode=0o600, exist_ok=False)
        os.chown(output, app_user.pw_uid, app_user.pw_gid)
        os.chmod(output, 0o440)
        os.chown(output_dir, app_user.pw_uid, app_user.pw_gid)
        os.chmod(output_dir, 0o550)
        return 0

    digest = hashlib.sha256(POLICY.read_bytes()).hexdigest()
    product_digest = hashlib.sha256(PRODUCT_CATALOG.read_bytes()).hexdigest()
    if output.exists():
        content = output.read_text(encoding="ascii")
        if (
            f"A2A_AFTERSALES_POLICY_SHA256={digest}\n" in content
            and product_digest in content
            and any(
                line.startswith("A2A_AFTERSALES_TOKEN=") and len(line.split("=", 1)[1]) >= 64
                for line in content.splitlines()
            )
            and "MCP_ENABLED=true\n" in content
            and "A2A_AFTERSALES_INTERNAL_HTTP=true\n" in content
        ):
            return 0
        raise RuntimeError(
            "Existing private protocol config is stale or invalid; remove its volume deliberately"
        )

    token = secrets.token_urlsafe(48)
    content = (
        "APP_ENV=development\n"
        "MCP_ENABLED=true\n"
        "MCP_FAIL_CLOSED=true\n"
        "MCP_SERVERS="
        + shlex.quote(
            json.dumps(
                [
                    {
                        "name": "productdocs",
                        "transport": "stdio",
                        "command": "python3",
                        "args": [
                            "/app/tools/readonly_mcp_product_documents.py",
                            "--catalog",
                            "/app/deploy/fixtures/product-support-documents.synthetic.json",
                            "--sha256",
                            product_digest,
                            "--authorized-customer-disclosure",
                        ],
                        "allowed_tools": ["lookup_product_support_document"],
                        "risk_level": "low",
                        "timeout_seconds": 5,
                    }
                ],
                separators=(",", ":"),
            )
        )
        + "\n"
        "A2A_AFTERSALES_ENABLED=true\n"
        "A2A_AFTERSALES_INTERNAL_HTTP=true\n"
        "A2A_AFTERSALES_PRIVATE_HTTPS=false\n"
        "A2A_AFTERSALES_ENDPOINT=http://a2a-peer:41243\n"
        f"A2A_AFTERSALES_TOKEN={token}\n"
        f"A2A_AFTERSALES_POLICY_SHA256={digest}\n"
    )
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    # Only the unprivileged runtime identity can read the mounted file.
    os.chown(output, app_user.pw_uid, app_user.pw_gid)
    os.chmod(output, 0o440)
    os.chown(output_dir, app_user.pw_uid, app_user.pw_gid)
    os.chmod(output_dir, 0o550)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
