"""Format inventory is intentionally not an authentication or migration proof."""

import base64
import json

import pytest

from scripts.inventory_session_records import inventory, main


def test_inventory_counts_without_exporting_values(tmp_path, capsys):
    path = tmp_path / "private.jsonl"
    records = [
        "csae1:corrupt-token",
        base64.urlsafe_b64encode(b"\x80fixture").decode(),
        '{"private":"never-print-this"}',
        "unrecognized-private-text",
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    before = path.read_bytes()
    assert main(["--input", str(path)]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["total"] == 4
    assert set(report["counts"].values()) == {1}
    assert report["plaintext_provenance_verified"] is False
    assert report["ciphertext_authentication_verified"] is False
    assert report["safe_to_flip_reject_mode"] is False
    assert "private" not in output
    assert "corrupt-token" not in output
    assert path.read_bytes() == before


@pytest.mark.parametrize("record", ['{"private":"secret"}', "not-json-secret", "null"])
def test_invalid_export_fails_without_record_leak(tmp_path, capsys, record):
    path = tmp_path / "invalid.jsonl"
    path.write_text(record + "\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--input", str(path)])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "secret" not in captured.err


def test_empty_export_never_certifies_cutover(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.touch()
    report = inventory(path)
    assert report["total"] == 0
    assert report["safe_to_flip_reject_mode"] is False
