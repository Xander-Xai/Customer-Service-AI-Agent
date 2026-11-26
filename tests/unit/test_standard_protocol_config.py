import hashlib
import stat
from types import SimpleNamespace

import pytest

from scripts.prepare_standard_protocol_config import PRODUCT_CATALOG, main


def test_synthetic_protocol_config_is_private_and_reusable(tmp_path, monkeypatch):
    monkeypatch.setenv("CSAI_PROTOCOL_MODE", "synthetic")
    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "standard")
    monkeypatch.setattr("scripts.prepare_standard_protocol_config.os.chown", lambda *_: None)
    monkeypatch.setattr(
        "scripts.prepare_standard_protocol_config.pwd.getpwnam",
        lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000),
    )
    monkeypatch.setattr("sys.argv", ["prepare", "--output-directory", str(tmp_path)])
    assert main() == 0
    output = tmp_path / "protocol.env"
    text = output.read_text(encoding="ascii")
    assert "A2A_AFTERSALES_INTERNAL_HTTP=true" in text
    assert "MCP_ENABLED=true" in text
    assert "MCP_FAIL_CLOSED=true" in text
    assert "A2A_AFTERSALES_TOKEN=" in text
    assert hashlib.sha256(PRODUCT_CATALOG.read_bytes()).hexdigest() in text
    assert "--synthetic-fixture" in text
    assert "--authorized-customer-disclosure" not in text
    assert stat.S_IMODE(output.stat().st_mode) == 0o440
    token = next(
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith("A2A_AFTERSALES_TOKEN=")
    )
    assert len(token) >= 64
    assert main() == 0
    assert output.read_text(encoding="ascii") == text


def test_industrial_profile_rejects_synthetic_protocol_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CSAI_PROTOCOL_MODE", "synthetic")
    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "industrial")
    monkeypatch.setattr("sys.argv", ["prepare", "--output-directory", str(tmp_path)])
    with pytest.raises(RuntimeError, match="rejects synthetic"):
        main()
    assert not (tmp_path / "protocol.env").exists()


def test_external_protocol_config_contains_no_generated_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CSAI_PROTOCOL_MODE", "external")
    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "industrial")
    monkeypatch.setattr("scripts.prepare_standard_protocol_config.os.chown", lambda *_: None)
    monkeypatch.setattr(
        "scripts.prepare_standard_protocol_config.pwd.getpwnam",
        lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000),
    )
    monkeypatch.setattr(
        "scripts.prepare_standard_protocol_config.pwd.getpwnam",
        lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000),
    )
    monkeypatch.setattr("sys.argv", ["prepare", "--output-directory", str(tmp_path)])
    assert main() == 0
    assert (tmp_path / "protocol.env").read_text(encoding="ascii") == ""
    assert main() == 0
