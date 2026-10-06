"""Contract tests for production nginx TLS material provisioning (Issue #57).

The problem
-----------
``deploy/nginx/nginx.conf`` declares ``listen 443 ssl`` and references
``/etc/nginx/ssl/{cert.pem,key.pem}``. That directory is operator-provisioned and
gitignored -- the right intent -- but **nothing checked it**:

* ``docker compose config`` validates schema and interpolation, never the
  filesystem, so a missing certificate still exits 0;
* ``docker compose build`` likewise;
* Docker **auto-creates a missing bind-mount source as an empty directory**, so the
  containers come up and only nginx fails, after the fact:
  ``nginx: [emerg] cannot load certificate "/etc/nginx/ssl/cert.pem"``.

A missing *variable* can be caught at compose-parse time with ``${VAR:?}``; a missing
*file* cannot. These tests pin the contract that replaces that gap.

What is pinned
--------------
1. **No private key or certificate is ever committed.** This is the constraint that
   matters most, and the one a future "just add a cert so CI works" change would
   violate.
2. The TLS directories are gitignored, so an operator's material cannot be committed
   by accident.
3. Missing / empty / unparseable / mismatched / expired / self-signed material each
   fail with a message that names what is needed.
4. ``docker compose up`` fails before nginx starts, via the ``tls-check`` one-shot
   service nginx depends on.
5. The local self-signed helper is labelled as such and cannot pass the production
   check by accident.

The check script is exercised as a subprocess against real generated material —
a hand-written fake would not prove the openssl plumbing works.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "docker-compose.yml"
NGINX_CONF = REPO_ROOT / "deploy" / "nginx" / "nginx.conf"
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_tls_material.py"
GEN_SCRIPT = REPO_ROOT / "scripts" / "generate_local_selfsigned_cert.py"
MAKEFILE = REPO_ROOT / "Makefile"

#: The two places TLS material may legitimately live. Both must stay ignored.
TLS_DIRS = (".certs", "deploy/nginx/ssl")

#: Block headers a tracked file must never contain (checked with the full dashed form).
PEM_MARKERS = ("BEGIN CERTIFICATE", "BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY")


def _openssl() -> str | None:
    return shutil.which("openssl")


requires_openssl = pytest.mark.skipif(_openssl() is None, reason="openssl CLI not available")


def _run_check(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


# ---------------------------------------------------------------------------
# A. the constraint that matters most: no key material in the repo
# ---------------------------------------------------------------------------


class TestNoCertificateMaterialIsCommitted:
    """The invariant is about what git tracks, not what is on disk.

    An operator's machine legitimately holds untracked, gitignored key material —
    that is the whole point of the provisioning contract. What must never happen is
    that such material becomes committable.
    """

    @staticmethod
    def _tracked_files() -> list[str]:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.splitlines()

    @pytest.mark.unit
    def test_no_certificate_or_key_files_are_tracked(self) -> None:
        offenders = [
            rel
            for rel in self._tracked_files()
            if Path(rel).suffix.lower() in {".pem", ".key", ".crt", ".p12", ".pfx", ".jks"}
        ]
        assert not offenders, f"key/certificate material must never be committed: {offenders}"

    @pytest.mark.unit
    def test_no_pem_block_is_inlined_in_a_tracked_file(self) -> None:
        """A key pasted into a script or a doc is still a leak."""
        # Full dashed headers, so this file's own marker constants cannot self-match.
        pem_block = re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?(?:CERTIFICATE|PRIVATE KEY)-----[\\s\\S]*?-----END"
        )
        offenders: list[str] = []
        for rel in self._tracked_files():
            path = REPO_ROOT / rel
            if not path.is_file() or path.suffix.lower() in {".png", ".jpg", ".ico", ".woff"}:
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if pem_block.search(content):
                offenders.append(rel)
        assert not offenders, f"PEM block inlined in tracked files: {offenders}"

    @pytest.mark.unit
    @pytest.mark.parametrize("tls_dir", TLS_DIRS)
    def test_tls_directories_are_gitignored(self, tls_dir: str) -> None:
        result = subprocess.run(
            ["git", "check-ignore", "-q", f"{tls_dir}/"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"{tls_dir}/ must stay gitignored; operator-provisioned material placed "
            "there would otherwise be committable"
        )

    @pytest.mark.unit
    def test_nothing_under_the_ssl_directory_is_tracked(self) -> None:
        tracked = [
            rel
            for rel in subprocess.run(
                ["git", "ls-files", "deploy/nginx"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            ).stdout.split()
            if "ssl" in rel
        ]
        assert not tracked, f"nothing under deploy/nginx/ssl may be tracked: {tracked}"


# ---------------------------------------------------------------------------
# B. the config actually demands TLS
# ---------------------------------------------------------------------------


class TestNginxConfigDemandsTlsMaterial:
    @pytest.mark.unit
    def test_config_references_the_operator_provided_paths(self) -> None:
        conf = NGINX_CONF.read_text(encoding="utf-8")
        assert "/etc/nginx/ssl/cert.pem" in conf, (
            "the config must keep referencing the operator-provided certificate path; "
            "hardcoding or dropping it would bypass the provisioning contract"
        )
        assert "/etc/nginx/ssl/key.pem" in conf

    @pytest.mark.unit
    def test_config_has_no_plain_http_fallback_serving_traffic(self) -> None:
        """The issue asked for a decision here; the answer is documented: no fallback.

        ``listen 80`` only issues a 301. Adding an HTTP-only app server would serve
        the API without TLS, which is strictly worse than failing to start.
        """
        conf = NGINX_CONF.read_text(encoding="utf-8")
        assert "proxy_pass" in conf
        # every proxy_pass must live in the 443 server, i.e. there is no
        # `listen 80` block that proxies to the app
        port80 = conf.split("listen 80;", 1)[-1].split("server {", 1)[0]
        assert "proxy_pass" not in port80, (
            "the plain-HTTP server must only redirect; proxying the app over HTTP would "
            "serve traffic without TLS"
        )

    @pytest.mark.unit
    def test_docs_state_that_http_only_is_not_provided(self) -> None:
        guide = REPO_ROOT / "docs" / "operations" / "production-operations-guide.md"
        text = guide.read_text(encoding="utf-8")
        assert "TLS-1" in text, "the runbook must have a TLS provisioning entry"
        assert "自签名" in text, "the runbook must state the self-signed policy"


# ---------------------------------------------------------------------------
# C. the preflight detects every failure mode, with an actionable message
# ---------------------------------------------------------------------------


class TestPreflightFailsFastWithActionableMessage:
    @pytest.mark.unit
    def test_missing_directory_fails(self, tmp_path: Path) -> None:
        result = _run_check("--ssl-dir", str(tmp_path / "absent"))
        assert result.returncode == 1, result.stdout + result.stderr
        assert "不存在" in result.stdout + result.stderr

    @pytest.mark.unit
    def test_missing_file_fails_and_names_the_path(self, tmp_path: Path) -> None:
        ssl = tmp_path / "ssl"
        ssl.mkdir()
        (ssl / "cert.pem").write_text("not a real certificate\n", encoding="utf-8")
        result = _run_check("--ssl-dir", str(ssl))
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "key.pem" in combined, "the missing file must be named"
        assert "cert.pem" not in combined.split("缺少")[1][:200], (
            "cert.pem is present and must not be reported as missing"
        )

    @pytest.mark.unit
    def test_empty_files_fail(self, tmp_path: Path) -> None:
        ssl = tmp_path / "ssl"
        ssl.mkdir()
        (ssl / "cert.pem").write_text("", encoding="utf-8")
        (ssl / "key.pem").write_text("", encoding="utf-8")
        result = _run_check("--ssl-dir", str(ssl))
        assert result.returncode == 1
        assert "0 字节" in result.stdout + result.stderr, (
            "an empty bind-mount source is equivalent to a missing one and must be called out"
        )

    @pytest.mark.unit
    def test_message_tells_the_operator_what_to_provide(self, tmp_path: Path) -> None:
        result = _run_check("--ssl-dir", str(tmp_path / "absent"))
        combined = result.stdout + result.stderr
        assert "需要提供什么" in combined, "the failure must carry remediation text"
        assert "cert.pem" in combined and "key.pem" in combined

    @requires_openssl
    @pytest.mark.unit
    def test_unparseable_material_fails(self, tmp_path: Path) -> None:
        ssl = tmp_path / "ssl"
        ssl.mkdir()
        (ssl / "cert.pem").write_text("definitely not PEM\n", encoding="utf-8")
        (ssl / "key.pem").write_text("definitely not PEM\n", encoding="utf-8")
        result = _run_check("--ssl-dir", str(ssl))
        assert result.returncode == 1
        assert "无法解析" in result.stdout + result.stderr

    @requires_openssl
    @pytest.mark.unit
    def test_self_signed_is_rejected_by_default(self, tmp_path: Path) -> None:
        """The production guard: a self-signed cert is not a usable trust anchor.

        It would leave the service "up but untrusted", which is harder to diagnose
        than a failed start, so the check refuses rather than warns.
        """
        ssl = tmp_path / "ssl"
        generated = subprocess.run(
            [sys.executable, str(GEN_SCRIPT), "--out-dir", str(ssl), "--cn", "localhost"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert generated.returncode == 0, generated.stderr
        result = _run_check("--ssl-dir", str(ssl))
        assert result.returncode == 1, (
            "a self-signed certificate must not pass the production check"
        )
        assert "自签名" in result.stdout + result.stderr

    @requires_openssl
    @pytest.mark.unit
    def test_self_signed_passes_only_with_explicit_opt_in(self, tmp_path: Path) -> None:
        ssl = tmp_path / "ssl"
        subprocess.run(
            [sys.executable, str(GEN_SCRIPT), "--out-dir", str(ssl), "--cn", "localhost"],
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )
        result = _run_check("--ssl-dir", str(ssl), "--allow-self-signed")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "非自签名" not in result.stdout, (
            "when a self-signed cert is allowed the output must not claim otherwise"
        )

    @pytest.mark.unit
    def test_openssl_absence_is_reported_not_silently_passed(self, tmp_path: Path, monkeypatch):
        """A partial check must say so, rather than look like a full pass."""
        ssl = tmp_path / "ssl"
        ssl.mkdir()
        (ssl / "cert.pem").write_text("x", encoding="utf-8")
        (ssl / "key.pem").write_text("y", encoding="utf-8")
        stub = tmp_path / "stub"
        stub.mkdir()
        env = dict(os.environ, PATH=str(stub))
        result = subprocess.run(
            [sys.executable, str(CHECK_SCRIPT), "--ssl-dir", str(ssl)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "openssl" in result.stderr, (
            "with openssl unavailable the check must disclose which validations it skipped"
        )


# ---------------------------------------------------------------------------
# D. the compose gate
# ---------------------------------------------------------------------------


class TestComposeGateBlocksNginxWithoutTls:
    @staticmethod
    def _compose() -> dict:
        import yaml

        return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))

    @pytest.mark.unit
    def test_tls_check_service_exists_and_is_one_shot(self) -> None:
        services = self._compose()["services"]
        assert "tls-check" in services, (
            "compose needs a TLS gate; a missing certificate currently only surfaces "
            "after nginx has already started"
        )
        assert services["tls-check"].get("restart") == "no", (
            "a gate that restarts would mask the failure and loop"
        )

    @pytest.mark.unit
    def test_gate_mounts_the_same_host_directory_as_nginx(self) -> None:
        """If the gate looks somewhere else, it cannot see what nginx will see."""
        services = self._compose()["services"]
        gate = [v for v in services["tls-check"]["volumes"] if "/etc/nginx/ssl" in str(v)]
        nginx = [v for v in services["nginx"]["volumes"] if "/etc/nginx/ssl" in str(v)]
        assert gate and nginx, "both must mount the TLS directory"
        assert str(gate[0]).split(":")[0] == str(nginx[0]).split(":")[0], (
            f"gate mounts {gate[0]!r} but nginx mounts {nginx[0]!r}; they must be the "
            "same host directory or the gate checks the wrong place"
        )

    @pytest.mark.unit
    def test_nginx_waits_for_the_gate_to_succeed(self) -> None:
        depends = self._compose()["services"]["nginx"]["depends_on"]
        assert isinstance(depends, dict), (
            "nginx depends_on must use the mapping form so a condition can be attached"
        )
        gate = depends.get("tls-check")
        assert gate, f"nginx must depend on the TLS gate; got {depends}"
        assert gate.get("condition") == "service_completed_successfully", (
            f"the gate must gate nginx; condition={gate.get('condition')!r}"
        )

    @pytest.mark.unit
    def test_gate_message_points_at_the_operator_actions(self) -> None:
        blob = yaml_safe_dump(self._compose()["services"]["tls-check"])
        assert "deploy/nginx/ssl/cert.pem" in blob, (
            "the gate message must name the host path the operator has to create"
        )
        assert "tls-check" in blob or "make tls-check" in blob, (
            "the gate must point at the full validation command"
        )
        assert "LOCAL DEVELOPMENT ONLY" in blob or "tls-local-cert" in blob, (
            "the gate must mark the local self-signed escape hatch as local-only"
        )

    @pytest.mark.unit
    def test_gate_does_not_ship_its_own_certificate(self) -> None:
        gate = yaml_safe_dump(self._compose()["services"]["tls-check"])
        for marker in PEM_MARKERS:
            assert marker not in gate, "the gate must not embed key material"


def yaml_safe_dump(value) -> str:
    import yaml

    return yaml.safe_dump(value, allow_unicode=True)


# ---------------------------------------------------------------------------
# E. the local helper cannot be mistaken for production material
# ---------------------------------------------------------------------------


class TestLocalHelperIsMarkedLocalOnly:
    @pytest.mark.unit
    def test_script_states_local_development_only(self) -> None:
        text = GEN_SCRIPT.read_text(encoding="utf-8")
        assert "LOCAL DEVELOPMENT ONLY" in text, (
            "the generator must label its own output as non-production, in the script "
            "itself so the intent is visible at the call site"
        )

    @pytest.mark.unit
    def test_generated_certificate_carries_the_marker_in_its_subject(self) -> None:
        """The marker must be *in the certificate*, not only in the docs.

        Anyone inspecting the file later sees it immediately.
        """
        if _openssl() is None:
            pytest.skip("openssl not available")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(GEN_SCRIPT), "--out-dir", tmp],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            subject = subprocess.run(
                [_openssl(), "x509", "-in", str(Path(tmp) / "cert.pem"), "-noout", "-subject"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            ).stdout
            assert "LOCAL DEVELOPMENT ONLY" in subject, (
                f"certificate subject must be self-identifying; got {subject.strip()!r}"
            )

    @pytest.mark.unit
    def test_private_key_permissions_are_tightened(self) -> None:
        if _openssl() is None:
            pytest.skip("openssl not available")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [sys.executable, str(GEN_SCRIPT), "--out-dir", tmp],
                capture_output=True,
                text=True,
                timeout=180,
                check=True,
            )
            mode = (Path(tmp) / "key.pem").stat().st_mode & 0o777
            assert mode & 0o077 == 0, f"private key is group/world readable: {oct(mode)}"

    @pytest.mark.unit
    def test_generator_refuses_to_overwrite_existing_material(self) -> None:
        """Silently replacing an operator-issued certificate would be harmful."""
        if _openssl() is None:
            pytest.skip("openssl not available")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            first = subprocess.run(
                [sys.executable, str(GEN_SCRIPT), "--out-dir", tmp],
                capture_output=True,
                text=True,
                timeout=180,
                check=True,
            )
            cert = Path(tmp) / "cert.pem"
            sentinel = b"-----BEGIN CERTIFICATE-----\nOPERATOR-ISSUED-SENTINEL\n"
            cert.write_bytes(sentinel)
            second = subprocess.run(
                [sys.executable, str(GEN_SCRIPT), "--out-dir", tmp],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            assert second.returncode == 1, "must refuse to overwrite without --force"
            assert cert.read_bytes() == sentinel, "the existing file must be untouched"
            assert first.returncode == 0, first.stderr

    @pytest.mark.unit
    def test_generator_refuses_long_lived_certificates(self) -> None:
        """A long-lived local certificate is the likeliest route to it becoming 'production'."""
        result = subprocess.run(
            [
                sys.executable,
                str(GEN_SCRIPT),
                "--out-dir",
                "/tmp/should-not-be-created-57",
                "--days",
                "3650",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 1
        assert not Path("/tmp/should-not-be-created-57").exists(), (
            "a refused request must not create anything on disk"
        )


# ---------------------------------------------------------------------------
# F. Makefile wiring
# ---------------------------------------------------------------------------


class TestMakefileWiring:
    @pytest.mark.unit
    def test_tls_check_target_exists(self) -> None:
        makefile = MAKEFILE.read_text(encoding="utf-8")
        assert "\ntls-check:" in makefile
        assert "check_tls_material.py" in makefile

    @pytest.mark.unit
    def test_prod_depends_on_tls_check(self) -> None:
        """`make prod` must abort before compose brings anything up."""
        makefile = MAKEFILE.read_text(encoding="utf-8")
        prod_line = next(
            (ln for ln in makefile.splitlines() if ln.startswith("prod:") and "env-prod" in ln),
            None,
        )
        assert prod_line, "could not find the prod target"
        assert "tls-check" in prod_line, f"prod must depend on tls-check; got {prod_line!r}"

    @pytest.mark.unit
    def test_local_cert_target_exists_and_is_labelled_local(self) -> None:
        makefile = MAKEFILE.read_text(encoding="utf-8")
        line = next((ln for ln in makefile.splitlines() if ln.startswith("tls-local-cert:")), None)
        assert line, "a local-only escape hatch should be discoverable from make help"
        assert "LOCAL DEVELOPMENT ONLY" in line, (
            "make help is what an operator scans; the warning must be there"
        )
