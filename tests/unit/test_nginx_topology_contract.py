"""Static topology contract for the nginx reverse proxy (Issue #59).

What was broken
---------------
Three defects, only the first of which the issue reported:

1. ``upstream`` and ``map`` were nested **inside** a ``server {}`` block. Both are
   ``http``-context directives, so the file did not parse at all --
   ``nginx: [emerg] "upstream" directive is not allowed here``. nginx could not
   start in *any* stack, which is why the other two defects were never observed.
2. The base config declared ``upstream canary_backend { server canary:8000; }``
   while ``canary`` only exists in ``docker-compose.canary.yml``.
3. ``location /assets/`` and ``/static/`` used ``alias /home/app/web/static/...``.
   That path exists in neither container: the nginx image only has ``/etc/nginx``,
   and the app image is ``WORKDIR /app`` with a ``appuser`` home, so there is no
   ``/home/app`` in either. Both locations were permanently 404.

Plus a fourth found while verifying: the canary and scale compose variants mounted
the config to ``/etc/nginx/nginx.conf`` (the *main* config, containing
``user``/``events``/``http``) instead of ``/etc/nginx/conf.d/default.conf`` where
the image's server config lives. The canary config was therefore never in effect.

What these tests pin
--------------------
- the structural rule that caused (1): ``upstream``/``map`` stay at http level
- base config references no canary service; the canary config does
- the canary config is mounted **only** by the canary variant, at the right target
- no ``alias``/``root`` in either config, and static paths are owned by the app
- the two files cannot drift apart on the security/timeout invariants
- base does not claim canary is enabled

The Docker-backed cases (``nginx -t`` and a live start) are gated on the docker
CLI, because a config that merely *parses* is not the same as a topology that
*resolves*.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_DIR = REPO_ROOT / "deploy" / "nginx"
BASE_CONF = NGINX_DIR / "nginx.conf"
CANARY_CONF = NGINX_DIR / "nginx.conf.canary"
NGINX_DOCKERFILE = NGINX_DIR / "Dockerfile"
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"

#: Where the image actually keeps its server config. Both the Dockerfile COPY and
#: every compose override must agree on this path.
CONF_DEST = "/etc/nginx/conf.d/default.conf"

#: The main nginx.conf -- NOT a valid mount target for a server-block file.
MAIN_CONF = "/etc/nginx/nginx.conf"

BASE_IMAGE = "nginx:1.27-alpine"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """Drop comments so assertions cannot be satisfied by prose alone."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("  #", 1)[0] if "  #" in line else line)
    return "\n".join(out)


def _top_level_blocks(text: str, keyword: str) -> list[str]:
    """Return the bodies of ``keyword`` blocks declared at http level (depth 0).

    Depth is tracked across ``{``/``}`` so a block nested inside ``server`` or
    ``location`` is *not* returned -- that distinction is the whole point.
    """
    bodies: list[str] = []
    depth = 0
    index = 0
    lines = text.splitlines()
    while index < len(lines):
        line = lines[index]
        if depth == 0 and re.match(rf"\s*{keyword}\b", line):
            body: list[str] = []
            brace_depth = line.count("{") - line.count("}")
            body.append(line)
            while brace_depth > 0 and index + 1 < len(lines):
                index += 1
                body.append(lines[index])
                brace_depth += lines[index].count("{") - lines[index].count("}")
            bodies.append("\n".join(body))
        depth += line.count("{") - line.count("}")
        index += 1
    return bodies


def _depth_of_first_match(text: str, pattern: str) -> int:
    """Brace depth at which the first line matching ``pattern`` appears."""
    depth = 0
    for line in text.splitlines():
        if re.search(pattern, line):
            return depth
        depth += line.count("{") - line.count("}")
    raise AssertionError(f"pattern not found: {pattern}")


def _compose(path: Path) -> dict:
    import yaml

    return yaml.safe_load(_read(path))


def _has_docker() -> bool:
    return shutil.which("docker") is not None


@pytest.fixture(scope="module")
def throwaway_certs(tmp_path_factory) -> Path:
    """Self-signed throwaway cert material for `nginx -t`.

    Issue #57 (deploy/nginx/ssl ships no certificate) is out of scope here, but
    without *some* material nginx refuses to load the ``listen 443 ssl`` server
    and the parse test would fail for a reason unrelated to what it checks. The
    certificates are generated here and never committed; they only need to be
    parseable, not trusted.
    """
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available to generate throwaway certificates")
    cert_dir = tmp_path_factory.mktemp("nginx-ssl")
    cert, key = cert_dir / "cert.pem", cert_dir / "key.pem"
    result = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=contract-test",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0 or not cert.is_file():
        pytest.skip(f"could not generate throwaway certificates: {result.stderr[:200]}")
    return cert_dir


# ---------------------------------------------------------------------------
# A. the structural defect: http-level directives must not be nested
# ---------------------------------------------------------------------------


class TestUpstreamAndMapLiveAtHttpLevel:
    """`upstream`/`map` inside `server {}` is a hard parse error.

    This is the defect that made nginx unstartable everywhere, and it is the one
    that silently hid the other two: nobody could reach the canary-upstream or
    alias problems while the file did not even parse.
    """

    @pytest.mark.unit
    @pytest.mark.parametrize("conf", [BASE_CONF, CANARY_CONF], ids=["base", "canary"])
    @pytest.mark.parametrize("keyword", ["upstream", "map"])
    def test_declared_at_top_level(self, conf: Path, keyword: str) -> None:
        text = _strip_comments(_read(conf))
        if not re.search(rf"^\s*{keyword}\b", text, re.M):
            pytest.skip(f"{conf.name} declares no {keyword}")
        assert _top_level_blocks(text, keyword), (
            f"{conf.name} declares a {keyword} block, but none at http level -- every "
            f"{keyword} found is nested inside server/location, which nginx rejects"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("conf", [BASE_CONF, CANARY_CONF], ids=["base", "canary"])
    def test_upstream_is_never_nested(self, conf: Path) -> None:
        text = _strip_comments(_read(conf))
        depth = _depth_of_first_match(text, r"^\s*upstream\s")
        assert depth == 0, (
            f"{conf.name}: first `upstream` sits at brace depth {depth}, i.e. inside a "
            'server/location block. nginx rejects this with "upstream directive is not '
            'allowed here" and the container never starts'
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("conf", [BASE_CONF, CANARY_CONF], ids=["base", "canary"])
    def test_map_is_never_nested(self, conf: Path) -> None:
        text = _strip_comments(_read(conf))
        if not re.search(r"^\s*map\s", text, re.M):
            pytest.skip("config declares no map")
        depth = _depth_of_first_match(text, r"^\s*map\s")
        assert depth == 0, f"{conf.name}: `map` must be at http level, found depth {depth}"

    @pytest.mark.unit
    @pytest.mark.parametrize("conf", [BASE_CONF, CANARY_CONF], ids=["base", "canary"])
    def test_config_does_not_declare_main_context_directives(self, conf: Path) -> None:
        """conf.d files are included *inside* http{}, so user/events/http are invalid."""
        text = _strip_comments(_read(conf))
        for directive in ("user ", "events ", "http {"):
            assert not re.search(rf"^\s*{re.escape(directive)}", text, re.M), (
                f"{conf.name} declares {directive!r}, which belongs in the main nginx.conf, "
                "not in a conf.d include"
            )


# ---------------------------------------------------------------------------
# B. base must not know about canary; canary must
# ---------------------------------------------------------------------------


class TestBaseConfigHasNoCanaryReference:
    @pytest.mark.unit
    def test_no_canary_upstream(self) -> None:
        """No upstream may be named after canary.

        Scoped to upstream/server references rather than the bare word: the
        ``/api/canary`` status endpoint legitimately exists in the base config (it
        reports ``disabled``). What must not exist is a *backend* that resolves to
        a service the base stack does not run.
        """
        text = _strip_comments(_read(BASE_CONF))
        offenders = _top_level_blocks(text, "upstream") + re.findall(
            r"^\s*server\s+\S+\s*:\s*\d+\s*;", text, re.M
        )
        for chunk in offenders:
            assert "canary" not in chunk.lower(), (
                "the base config references a canary backend, but `make prod` "
                f"(base + prod) deploys no canary service:\n{chunk}"
            )

    @pytest.mark.unit
    def test_no_canary_upstream_host(self) -> None:
        assert not re.search(r"server\s+canary\s*:", _strip_comments(_read(BASE_CONF)))

    @pytest.mark.unit
    def test_base_proxy_target_is_the_stable_upstream(self) -> None:
        text = _strip_comments(_read(BASE_CONF))
        assert re.search(r"proxy_pass\s+http://stable_backend\s*;", text), (
            "base has a single backend and must proxy straight to stable_backend "
            "(no map indirection needed)"
        )

    @pytest.mark.unit
    def test_base_does_not_claim_canary_is_enabled(self) -> None:
        """The status endpoint must tell the truth in the base stack."""
        text = _strip_comments(_read(BASE_CONF))
        match = re.search(r'\{\\?"canary\\?":\s*\\?"(\w+)', text)
        assert match, "base config must keep the /api/canary status endpoint"
        assert match.group(1) == "disabled", (
            f"base reports canary={match.group(1)!r}; the base stack deploys no canary, so "
            "reporting 'enabled' would make a false conclusion available"
        )


class TestCanaryConfigDeclaresBothBackends:
    @pytest.mark.unit
    def test_canary_upstream_points_at_the_canary_service(self) -> None:
        text = _strip_comments(_read(CANARY_CONF))
        assert re.search(r"server\s+canary\s*:\s*8000\s*;", text), (
            "the canary config must declare `server canary:8000` -- that is the service "
            "docker-compose.canary.yml defines"
        )

    @pytest.mark.unit
    def test_stable_upstream_still_declared(self) -> None:
        text = _strip_comments(_read(CANARY_CONF))
        assert re.search(r"server\s+app\s*:\s*8000\s*;", text), (
            "the canary stack also serves the stable app; traffic without the cookie "
            "must still reach it"
        )

    @pytest.mark.unit
    def test_cookie_drives_the_split(self) -> None:
        text = _strip_comments(_read(CANARY_CONF))
        blocks = _top_level_blocks(text, "map")
        assert blocks, "canary config must declare an http-level map"
        joined = "\n".join(blocks)
        assert "$cookie_canary_token" in joined, "the split must key off the canary cookie"
        assert "canary_backend" in joined and "stable_backend" in joined, (
            "both backends must appear as map results, or the cookie cannot select one"
        )

    @pytest.mark.unit
    def test_canary_proxy_uses_the_mapped_backend(self) -> None:
        text = _strip_comments(_read(CANARY_CONF))
        assert re.search(r"proxy_pass\s+http://\$\w+\s*;", text), (
            "the canary config must proxy through the mapped variable"
        )


# ---------------------------------------------------------------------------
# C. canary config is canary-only, and lands in the right place
# ---------------------------------------------------------------------------


def _nginx_volume_mounts(compose_path: Path) -> list[tuple[str, str]]:
    """Return ``[(host_path, container_path)]`` for the nginx service."""
    volumes = _compose(compose_path).get("services", {}).get("nginx", {}).get("volumes") or []
    mounts: list[tuple[str, str]] = []
    for entry in volumes:
        parts = str(entry).split(":")
        if len(parts) >= 2:
            mounts.append((parts[0], parts[1]))
    return mounts


class TestCanaryConfigIsWiredOnlyIntoTheCanaryStack:
    @pytest.mark.unit
    def test_canary_compose_mounts_the_canary_config(self) -> None:
        mounts = _nginx_volume_mounts(COMPOSE_DIR / "docker-compose.canary.yml")
        hosts = [h for h, _ in mounts]
        assert any(h.endswith("nginx.conf.canary") for h in hosts), (
            f"the canary variant must mount nginx.conf.canary; got {hosts}"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "compose_name",
        ["docker-compose.yml", "docker-compose.prod.yml", "docker-compose.monitoring.yml"],
    )
    def test_non_canary_variants_never_mount_the_canary_config(self, compose_name: str) -> None:
        compose_path = COMPOSE_DIR / compose_name
        if "nginx" not in _compose(compose_path).get("services", {}):
            pytest.skip(f"{compose_name} defines no nginx service")
        hosts = [h for h, _ in _nginx_volume_mounts(compose_path)]
        assert not any("nginx.conf.canary" in h for h in hosts), (
            f"{compose_name} must not mount the canary config; the canary service does "
            f"not exist in that stack. Got {hosts}"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "compose_name",
        [
            "docker-compose.canary.yml",
            "docker-compose.scale.yml",
        ],
        ids=["canary", "scale"],
    )
    def test_nginx_config_mounts_land_on_the_image_config_path(self, compose_name: str) -> None:
        """A server-block file mounted onto the *main* config cannot work.

        nginx.conf holds ``user``/``events``/``http``; replacing it with a
        ``server {}`` file yields "server directive is not allowed here", and the
        image's own conf.d/default.conf silently stays in charge -- so the mount
        appears to work while doing nothing.
        """
        mounts = _nginx_volume_mounts(COMPOSE_DIR / compose_name)
        offenders = [(h, c) for h, c in mounts if "nginx.conf" in h and c == MAIN_CONF]
        assert not offenders, (
            f"{compose_name} mounts a server-block config onto {MAIN_CONF} (the main "
            f"config). It must target {CONF_DEST}: {offenders}"
        )

    @pytest.mark.unit
    def test_dockerfile_puts_the_base_config_where_the_mounts_expect(self) -> None:
        dockerfile = _read(NGINX_DOCKERFILE)
        copy_lines = [
            ln.strip() for ln in dockerfile.splitlines() if ln.strip().startswith("COPY nginx.conf")
        ]
        assert copy_lines, "the Dockerfile must COPY the base config into the image"
        assert any(CONF_DEST in ln for ln in copy_lines), (
            f"the Dockerfile must place nginx.conf at {CONF_DEST} so the image default and "
            f"the compose overrides agree; got {copy_lines}"
        )


# ---------------------------------------------------------------------------
# D. static asset ownership: the app, not nginx
# ---------------------------------------------------------------------------


class TestStaticAssetsAreOwnedByTheApp:
    """No alias to a path that does not exist in the nginx container."""

    @pytest.mark.unit
    @pytest.mark.parametrize("conf", [BASE_CONF, CANARY_CONF], ids=["base", "canary"])
    def test_no_alias_directive(self, conf: Path) -> None:
        text = _strip_comments(_read(conf))
        assert not re.search(r"^\s*alias\s", text, re.M), (
            f"{conf.name} still uses `alias`; every previous target pointed inside the app "
            "image, which the nginx container does not have"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("conf", [BASE_CONF, CANARY_CONF], ids=["base", "canary"])
    def test_no_app_image_paths(self, conf: Path) -> None:
        text = _strip_comments(_read(conf))
        assert "/home/app" not in text, (
            f"{conf.name} references /home/app, which exists in neither container "
            "(nginx image: /etc/nginx only; app image: WORKDIR /app)"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("conf", [BASE_CONF, CANARY_CONF], ids=["base", "canary"])
    def test_static_paths_fall_through_to_the_app(self, conf: Path) -> None:
        """With no dedicated location, /assets/ and /static/ reach the app.

        nginx matches the longest prefix, so if neither path has its own
        ``location``, both are handled by ``location /`` -> proxy_pass.
        """
        text = _strip_comments(_read(conf))
        for path in ("/assets", "/static"):
            assert not re.search(rf"location\s+{path}/", text), (
                f"{conf.name} still has a dedicated location for {path}/; it must fall "
                "through to the app"
            )
        assert re.search(r"location\s+/\s*\{", text), "there must be a catch-all location /"

    @pytest.mark.unit
    def test_app_really_serves_those_paths_with_the_same_cache_headers(self) -> None:
        """Ownership is only 'clear' if the app actually provides what nginx gave up.

        The old aliases set ``Cache-Control: public, max-age=31536000, immutable``
        and ``X-Content-Type-Options: nosniff``. If the app did not serve the path,
        or served it without those headers, dropping the alias would be a regression
        rather than a correction.
        """
        app_source = _read(REPO_ROOT / "api" / "app.py")
        for mount_point in ('"/static"', '"/styles"', '"/assets"'):
            assert f"app.mount({mount_point}" in app_source, (
                f"the app must mount {mount_point} -- nginx no longer serves it"
            )
        assert "immutable" in app_source, (
            "the app's static wrapper must set the immutable cache header"
        )
        assert "nosniff" in app_source, "the app's static wrapper must set nosniff"

    @pytest.mark.unit
    def test_vite_output_is_a_build_artifact_not_committed(self) -> None:
        """Why nginx cannot simply COPY the assets: the path is not in a clean clone."""
        dist = REPO_ROOT / "web" / "static" / "dist"
        tracked = subprocess.run(
            ["git", "ls-files", str(dist.relative_to(REPO_ROOT))],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        assert not tracked, (
            "web/static/dist is expected to be a build artifact; if it is now committed, "
            "copying the assets into the nginx image becomes viable and the ownership "
            "decision should be revisited"
        )


# ---------------------------------------------------------------------------
# E. the two files must not drift
# ---------------------------------------------------------------------------


class TestBaseAndCanaryShareInvariants:
    """Both configs terminate TLS with the same posture; divergence is a silent hole."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "directive",
        [
            "Strict-Transport-Security",
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Content-Security-Policy",
            "Referrer-Policy",
        ],
    )
    def test_security_header_present_in_both(self, directive: str) -> None:
        for conf in (BASE_CONF, CANARY_CONF):
            assert directive in _strip_comments(_read(conf)), (
                f"{conf.name} is missing the {directive} security header; if this is "
                "intentional, the divergence must be deliberate and documented"
            )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "pattern",
        [
            r"ssl_protocols\s+TLSv1\.2 TLSv1\.3\s*;",
            r"proxy_read_timeout\s+300s\s*;",
            r"proxy_send_timeout\s+300s\s*;",
            r"proxy_connect_timeout\s+10s\s*;",
            r"client_max_body_size\s+1m\s*;",
            r"proxy_set_header\s+Upgrade\s+\$http_upgrade\s*;",
        ],
        ids=["tls", "read-timeout", "send-timeout", "connect-timeout", "body-size", "websocket"],
    )
    def test_tuning_is_identical(self, pattern: str) -> None:
        for conf in (BASE_CONF, CANARY_CONF):
            assert re.search(pattern, _strip_comments(_read(conf))), (
                f"{conf.name} does not match {pattern!r}; the base and canary configs must "
                "agree on TLS and proxy tuning"
            )

    @pytest.mark.unit
    def test_health_endpoint_never_routes_through_canary(self) -> None:
        """A stable outage must be reported, not masked by a healthy canary."""
        for conf in (BASE_CONF, CANARY_CONF):
            text = _strip_comments(_read(conf))
            block = re.search(r"location\s*=\s*/api/health\s*\{(.*?)\n    \}", text, re.S)
            assert block, f"{conf.name} must keep an /api/health location"
            assert "stable_backend" in block.group(1), (
                f"{conf.name}: /api/health must proxy to stable_backend"
            )


# ---------------------------------------------------------------------------
# F. Docker-backed: does it actually parse and resolve?
# ---------------------------------------------------------------------------

requires_docker = pytest.mark.skipif(not _has_docker(), reason="docker CLI not available")


@pytest.fixture(scope="module")
def upstream_network():
    """A docker network on which the Compose service names actually resolve.

    ``nginx -t`` resolves upstream hostnames at config-test time, so a config that
    says ``app:8000`` cannot be validated on a host where ``app`` does not resolve.

    This fixture exists because the absence of that resolution is invisible on some
    machines and fatal on others: a developer host whose resolver happens to answer
    ``app`` (a stray DNS/ VPN entry is enough) makes the config test pass, while CI
    fails with ``host not found in upstream "app:8000"``. Depending on ambient DNS
    made this suite environment-dependent, so the names are now provided explicitly
    and the check is hermetic.

    Yields the network name; tears down the network and its stand-ins afterwards.
    """
    net = f"nginx-topology-contract-{os.getpid()}"
    stand_ins: list[str] = []
    subprocess.run(["docker", "network", "rm", "-f", net], capture_output=True, check=False)
    created = subprocess.run(
        ["docker", "network", "create", net], capture_output=True, text=True, check=False
    )
    if created.returncode != 0:
        pytest.skip(f"cannot create a docker network: {created.stderr.strip()[:160]}")
    try:
        # One container per upstream name; they only need to exist in DNS.
        for alias in ("app", "canary"):
            name = f"{net}-{alias}"
            run = subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--network",
                    net,
                    "--network-alias",
                    alias,
                    "--entrypoint",
                    "sh",
                    BASE_IMAGE,
                    "-c",
                    "sleep 600",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if run.returncode != 0:
                pytest.skip(f"cannot start a stand-in for {alias!r}: {run.stderr.strip()[:160]}")
            stand_ins.append(name)
        yield net
    finally:
        for name in stand_ins:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", "-f", net], capture_output=True, check=False)


def _nginx_t(
    conf: Path | None,
    conf_dest: str = CONF_DEST,
    cert_dir: Path | None = None,
    network: str | None = None,
) -> subprocess.CompletedProcess:
    """Run ``nginx -t`` with ``conf`` optionally bind-mounted over the image config.

    ``conf=None`` exercises the base config exactly as the image ships it.
    """
    args = ["docker", "run", "--rm"]
    if network:
        args += ["--network", network]
    if conf is not None:
        args += ["-v", f"{conf}:{conf_dest}:ro"]
    if cert_dir is not None:
        args += ["-v", f"{cert_dir}:/etc/nginx/ssl:ro"]
    args += [BASE_IMAGE, "nginx", "-t"]
    return subprocess.run(args, capture_output=True, text=True, timeout=180, check=False)


@requires_docker
class TestConfigsAreAcceptedByNginx:
    @pytest.mark.unit
    def test_base_config_parses_as_shipped_in_the_image(
        self, throwaway_certs: Path, upstream_network: str
    ) -> None:
        result = _nginx_t(None, cert_dir=throwaway_certs, network=upstream_network)
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"base config rejected by nginx:\n{combined}"
        assert "directive is not allowed here" not in combined, (
            f"a directive is in the wrong context:\n{combined}"
        )

    @pytest.mark.unit
    def test_canary_config_parses_when_mounted_like_the_canary_compose(
        self, throwaway_certs: Path, upstream_network: str
    ) -> None:
        result = _nginx_t(CANARY_CONF, cert_dir=throwaway_certs, network=upstream_network)
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"canary config rejected by nginx:\n{combined}"

    @pytest.mark.unit
    def test_mounting_onto_the_main_config_is_rejected(
        self, throwaway_certs: Path, upstream_network: str
    ) -> None:
        """Documents *why* the mount target is pinned, by showing the failure.

        Guards against someone 'simplifying' the target back to
        /etc/nginx/nginx.conf because it looks more obvious.
        """
        result = _nginx_t(
            BASE_CONF, conf_dest=MAIN_CONF, cert_dir=throwaway_certs, network=upstream_network
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            "expected mounting a server-block file onto the main config to fail; if nginx "
            f"now accepts it, revisit the pinned target:\n{combined}"
        )
        assert "not allowed here" in combined, f"unexpected failure mode:\n{combined}"
