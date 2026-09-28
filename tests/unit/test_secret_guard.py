from __future__ import annotations

from scripts.check_secrets import scan_text


def test_secret_guard_detects_private_key_without_exposing_content() -> None:
    marker = "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5
    findings = scan_text("fixture.pem", marker + "\nnot-a-real-key\n")

    assert [(finding.location, finding.category) for finding in findings] == [
        ("fixture.pem", "private-key-material")
    ]


def test_secret_guard_allows_public_certificate_text_and_placeholders() -> None:
    certificate = "-" * 5 + "BEGIN CERTIFICATE" + "-" * 5
    text = certificate + "\nexample-key-placeholder\nhttps://example.com\n"

    assert scan_text("fixture.crt", text) == []
