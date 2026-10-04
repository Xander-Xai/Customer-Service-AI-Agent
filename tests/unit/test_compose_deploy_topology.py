"""Regression tests for the deployment topology (issue #48).

Root cause these tests pin
-------------------------
``deploy/compose/docker-compose.yml`` declared the nginx build context as
``../../nginx``. Compose resolves a relative context against **the directory of
the compose file** (``deploy/compose/``), so that path is ``<repo>/nginx`` -- a
directory that does not exist. The real build context is ``deploy/nginx/``
(``Dockerfile`` + ``nginx.conf``). Because the context cannot be prepared,
``docker compose build`` aborts before producing anything:

    unable to prepare context: path "/<repo>/nginx" not found

That hard-blocked every documented production entry point (``make prod``,
README 方式一 / 方式二, ``docs/operations/production-operations-guide.md``) at
step one. ``README.md`` §项目结构 agreed with the broken path rather than with
the repository: it listed ``nginx/`` and ``loki/`` at the repo root while only
``monitoring/`` is actually there.

A second, quieter half of the same defect: the ports the docs tell operators to
use are not the ports Compose publishes. Only ``nginx`` publishes an
application port (80/443). ``app`` and ``prometheus`` use ``expose:`` only, so
``http://localhost:8000`` and ``http://localhost:9090`` are unreachable from the
host in the production stack -- yet README, the CI deploy smoke and the
production runbook all used them.

Scope of these tests (deliberately Docker-free, per repo convention)
-------------------------------------------------------------------
They pin the *contract* of the Compose files and the operator-facing docs:

1. every build context resolves on disk (this is the #48 regression itself)
2. paths into the ``deploy/`` subtree are anchored one hop (``../``) from
   ``deploy/compose/``, never spelled ``../../deploy/``
3. every bind-mount source is either present in the repo or explicitly declared
   operator-provisioned in ``.gitignore`` (TLS material)
4. the README project tree matches the real directory layout
5. README / CI / production guide agree with the ports Compose actually
   publishes, and reach non-published ports only from inside the network

They do not assert container runtime behaviour. The acceptance for that is
``docker compose config`` plus ``docker compose build``; TLS material
provisioning is tracked separately.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
COMPOSE_FILES = sorted(COMPOSE_DIR.glob("docker-compose*.yml"))
BASE_COMPOSE = COMPOSE_DIR / "docker-compose.yml"
PROD_COMPOSE = COMPOSE_DIR / "docker-compose.prod.yml"
GITIGNORE = REPO_ROOT / ".gitignore"
README = REPO_ROOT / "README.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PROD_GUIDE = REPO_ROOT / "docs" / "operations" / "production-operations-guide.md"

#: Ports that the production stack deliberately keeps off the host. ``app`` and
#: ``prometheus`` use ``expose:``, so they are reachable by service name inside
#: the compose network only. Publishing them would bypass nginx and drop TLS,
#: the security headers and the canary traffic split (for ``app``), and would
#: expose an unauthenticated metrics endpoint (for ``prometheus``).
CONTAINER_ONLY_PORTS = {"8000": "app", "9090": "prometheus"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _build_context(service: dict) -> str | None:
    build = service.get("build")
    if isinstance(build, str):
        return build
    if isinstance(build, dict):
        return build.get("context")
    return None


def _host_ports(paths: list[Path]) -> dict[str, set[int]]:
    """Map service name -> set of container ports published on the host.

    Handles the interpolated form the stack actually uses,
    ``"${NGINX_HTTP_PORT:-80}:80"``. Splitting on the *last* colon keeps the
    ``:-`` default inside the host-side expression intact; publishing is then
    asserted against that default, because that is what an operator gets from a
    stock ``.env``.
    """
    published: dict[str, set[int]] = {}
    for path in paths:
        for name, service in (_load(path).get("services") or {}).items():
            for entry in service.get("ports") or []:
                host_part = str(entry).rsplit(":", 1)[0]
                default = re.search(r":-(\d+)\}", host_part)
                number = default or re.fullmatch(r"\s*(\d+)(?:-\d+)?\s*", host_part)
                if number:
                    published.setdefault(name, set()).add(int(number.group(1)))
    return published


def _all_named_volumes() -> set[str]:
    """Every named volume declared anywhere under ``deploy/compose/``.

    The variant files are overlays and are not resolvable standalone -- the
    monitoring variant references the base file's ``app-logs`` volume without
    declaring it (docker-compose.otel.yml documents this explicitly). So the
    named-volume set has to be the union, not the per-file one.
    """
    named: set[str] = set()
    for path in COMPOSE_FILES:
        named |= set(_load(path).get("volumes") or {})
    return named


def _bind_mount_sources(path: Path) -> list[tuple[str, str]]:
    """Return ``(service, host_source)`` for every *bind* mount with a relative source.

    Named volumes (``app-logs``, ``loki-data``, ...) are not host paths and are
    excluded against the union of declared volume names.
    """
    doc = _load(path)
    named = _all_named_volumes()
    out: list[tuple[str, str]] = []
    for name, service in (doc.get("services") or {}).items():
        for entry in service.get("volumes") or []:
            if not isinstance(entry, str):
                continue
            source = entry.split(":")[0].strip()
            if not source or source.startswith(("/", "$")) or source in named:
                continue
            out.append((name, source))
    return out


def _ignored_prefixes() -> set[str]:
    """Path prefixes that ``.gitignore`` declares as operator-provisioned."""
    prefixes = set()
    for line in GITIGNORE.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or entry.startswith("!"):
            continue
        prefixes.add(entry.rstrip("/"))
    return prefixes


def _join_continuations(text: str) -> list[str]:
    """One logical line per shell statement, joining ``\\`` continuations."""
    joined: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            buffer = (
                f"{buffer} {stripped[:-1].rstrip()}".strip() if buffer else stripped[:-1].rstrip()
            )
            continue
        joined.append(f"{buffer} {stripped}".strip() if buffer else stripped)
        buffer = ""
    if buffer:
        joined.append(buffer)
    return joined


def _ci_deploy_script_lines() -> list[str]:
    """Shell statements of the CI ``deploy`` job's remote script.

    Read from the parsed workflow rather than by grepping, so the scan cannot be
    satisfied by an unrelated ``localhost:8000`` elsewhere in the file. The
    script lives under ``with:`` (it is an argument to ``appleboy/ssh-action``),
    not at the step's top level -- reading only ``step["script"]`` finds nothing
    and the assertion would pass vacuously.
    """
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    statements: list[str] = []
    for step in workflow.get("jobs", {}).get("deploy", {}).get("steps") or []:
        for candidate in (step.get("script"), (step.get("with") or {}).get("script")):
            if isinstance(candidate, str):
                statements.extend(_join_continuations(candidate))
    assert statements, (
        "no deploy script found in .github/workflows/ci.yml; the port-semantics "
        "check for CI would pass vacuously"
    )
    return statements


def _shell_lines(text: str) -> list[str]:
    """Logical lines from shell code blocks only.

    The invariant under test is about *commands*: a container-only port may only
    be dialled from inside the Compose network. Prose and reference tables
    legitimately mention the same port as a container-internal address -- the
    port-semantics table in this very guide does -- so they are excluded rather
    than silently exempted line by line.
    """
    inside = False
    shell_lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            if inside and buffer:
                shell_lines.append(buffer)
                buffer = ""
            info = stripped[3:].strip().lower()
            inside = info in {"", "bash", "sh", "shell", "console"}
            continue
        if not inside:
            continue
        if stripped.endswith("\\"):
            buffer = (
                f"{buffer} {stripped[:-1].rstrip()}".strip() if buffer else stripped[:-1].rstrip()
            )
            continue
        shell_lines.append(f"{buffer} {stripped}".strip() if buffer else stripped)
        buffer = ""
    if inside and buffer:
        shell_lines.append(buffer)
    return shell_lines


# --------------------------------------------------------------------------
# 1. the #48 regression: every build context must exist
# --------------------------------------------------------------------------
class TestBuildContextsResolve:
    @pytest.mark.parametrize(
        "compose_file",
        COMPOSE_FILES,
        ids=lambda p: p.name,
    )
    def test_every_build_context_exists_on_disk(self, compose_file: Path) -> None:
        """``docker compose build`` aborts on an unresolvable context.

        This is the exact #48 failure: ``build: ../../nginx`` pointed at
        ``<repo>/nginx``, which does not exist, so the build died with
        "unable to prepare context" before creating anything.
        """
        missing = []
        for name, service in (_load(compose_file).get("services") or {}).items():
            context = _build_context(service)
            if context is None:
                continue
            resolved = (compose_file.parent / context).resolve()
            if not resolved.is_dir():
                missing.append(f"{name}: {context} -> {resolved}")
        assert not missing, (
            f"{compose_file.relative_to(REPO_ROOT)} declares build contexts that do "
            "not exist; `docker compose build` aborts with 'unable to prepare "
            f"context': {'; '.join(missing)}"
        )

    def test_nginx_build_context_points_at_the_directory_holding_the_dockerfile(self) -> None:
        """The context must be the directory that actually contains the Dockerfile."""
        context = _build_context(_load(BASE_COMPOSE)["services"]["nginx"])
        resolved = (BASE_COMPOSE.parent / context).resolve()
        assert (resolved / "Dockerfile").is_file(), (
            f"nginx build context resolves to {resolved}, which has no Dockerfile"
        )
        assert (resolved / "nginx.conf").is_file(), (
            f"nginx build context resolves to {resolved}, which has no nginx.conf; "
            "the Dockerfile COPY would fail"
        )


# --------------------------------------------------------------------------
# 2. path anchor convention for the deploy/ subtree
# --------------------------------------------------------------------------
class TestDeploySubtreePathAnchor:
    @pytest.mark.parametrize(
        "compose_file",
        COMPOSE_FILES,
        ids=lambda p: p.name,
    )
    def test_no_path_is_anchored_through_a_redundant_deploy_hop(self, compose_file: Path) -> None:
        """``../../deploy/x`` from ``deploy/compose/`` means ``<repo>/deploy/x``.

        It resolves, but it spells the same directory two different ways in the
        same directory, which is how ``../../nginx`` came to be believed in the
        first place. Inside ``deploy/compose/`` the anchor is ``..``.
        """
        offenders = [
            f"{service}: {source}"
            for service, source in _bind_mount_sources(compose_file)
            if source.startswith("../../deploy/")
        ]
        assert not offenders, (
            f"{compose_file.relative_to(REPO_ROOT)} spells deploy/-subtree mounts as "
            f"'../../deploy/...'; use '../<name>' from deploy/compose/: {offenders}"
        )

    def test_compose_directory_is_the_only_anchor_for_deploy_assets(self) -> None:
        """deploy/nginx, deploy/loki and deploy/otel are the real asset locations."""
        for asset in (
            "nginx/Dockerfile",
            "nginx/nginx.conf",
            "loki/loki-config.yaml",
            "loki/promtail-config.yaml",
            "otel/collector-config.yaml",
        ):
            assert (REPO_ROOT / "deploy" / asset).is_file(), (
                f"deploy/{asset} is missing; the compose file references it as a "
                "build context or bind mount"
            )


# --------------------------------------------------------------------------
# 3. bind-mount sources are tracked or explicitly operator-provisioned
# --------------------------------------------------------------------------
class TestBindMountSourcesAreAccountedFor:
    @pytest.mark.parametrize(
        "compose_file",
        COMPOSE_FILES,
        ids=lambda p: p.name,
    )
    def test_each_mount_source_exists_or_is_declared_operator_provisioned(
        self, compose_file: Path
    ) -> None:
        ignored = _ignored_prefixes()
        unresolved = []
        for service, source in _bind_mount_sources(compose_file):
            resolved = (compose_file.parent / source).resolve()
            if resolved.exists():
                continue
            try:
                repo_relative = str(resolved.relative_to(REPO_ROOT))
            except ValueError:
                unresolved.append(f"{service}: {source} (outside the repository)")
                continue
            if not any(repo_relative == p or repo_relative.startswith(p + "/") for p in ignored):
                unresolved.append(f"{service}: {source} -> {repo_relative}")
        assert not unresolved, (
            f"{compose_file.relative_to(REPO_ROOT)} mounts host paths that are neither "
            "present in the repo nor declared in .gitignore as operator-provisioned: "
            f"{unresolved}"
        )


# --------------------------------------------------------------------------
# 4. README project tree matches the repository
# --------------------------------------------------------------------------
_TREE_BLOCK = re.compile(r"^customer-service-ai-agent/\n(.*?)\n```", re.M | re.S)
_ROOT_ENTRY = re.compile(r"^(?:├── |└── )([A-Za-z0-9_.\-]+/)")
_NESTED_ENTRY = re.compile(r"^(?:[│ ]*)(?:├── |└── )([A-Za-z0-9_.\-]+/)")


def _readme_tree_entries() -> list[tuple[int, str]]:
    """Return ``(depth, name)`` for every directory entry in the README tree."""
    match = _TREE_BLOCK.search(README.read_text(encoding="utf-8"))
    assert match, "README project tree block not found"
    entries: list[tuple[int, str]] = []
    for line in match.group(1).splitlines():
        root = _ROOT_ENTRY.match(line)
        if root:
            entries.append((0, root.group(1)))
            continue
        nested = _NESTED_ENTRY.match(line)
        if nested:
            depth = len(line) - len(line.lstrip("│ ")) // 4
            entries.append((max(depth, 1), nested.group(1)))
    return entries


class TestReadmeProjectTreeMatchesRepository:
    def test_every_root_level_entry_exists(self) -> None:
        missing = [
            name
            for depth, name in _readme_tree_entries()
            if depth == 0 and not (REPO_ROOT / name).is_dir()
        ]
        assert not missing, (
            f"README §项目结构 lists directories that do not exist at the repo root: "
            f"{missing}. This is the #48 documentation half: the tree agreed with the "
            "broken compose path and disagreed with the repository."
        )

    def test_nginx_and_loki_are_listed_under_deploy(self) -> None:
        entries = _readme_tree_entries()
        root_names = {name for depth, name in entries if depth == 0}
        assert "nginx/" not in root_names, (
            "nginx/ is not at the repo root; it lives at deploy/nginx/ (Dockerfile + nginx.conf)"
        )
        assert "loki/" not in root_names, (
            "loki/ is not at the repo root; it lives at deploy/loki/ "
            "(loki-config.yaml + promtail-config.yaml)"
        )
        assert "deploy/" in root_names, "README tree must list deploy/ as the asset root"

    def test_tree_does_not_claim_a_stale_alembic_version_count(self) -> None:
        """The tree pinned '3 个版本'; the repo carries 7 revisions."""
        text = README.read_text(encoding="utf-8")
        actual = len(
            [
                p
                for p in (REPO_ROOT / "alembic" / "versions").glob("*.py")
                if not p.name.startswith("__")
            ]
        )
        claimed = {int(n) for n in re.findall(r"alembic/.*?（(\d+)\s*个版本", text)}
        assert claimed == {actual}, (
            f"README claims {sorted(claimed)} alembic revisions but the repository has "
            f"{actual}. Derive this count, do not hand-maintain it."
        )


# --------------------------------------------------------------------------
# 5. one port semantics across compose / README / CI / production guide
# --------------------------------------------------------------------------
class TestProductionStackPortSemantics:
    def test_app_is_not_published_on_the_host(self) -> None:
        published = _host_ports([BASE_COMPOSE, PROD_COMPOSE])
        assert "app" not in published or 8000 not in published.get("app", set()), (
            "app must stay expose-only in the production stack: publishing 8000 "
            "bypasses nginx and loses TLS, the security headers and the canary split"
        )

    def test_prometheus_is_not_published_on_the_host(self) -> None:
        published = _host_ports([BASE_COMPOSE, PROD_COMPOSE])
        assert 9090 not in published.get("prometheus", set()), (
            "prometheus must stay expose-only: its HTTP API is unauthenticated and "
            "must not be reachable from outside the Docker network"
        )

    def test_nginx_is_published_as_the_application_ingress(self) -> None:
        published = _host_ports([BASE_COMPOSE, PROD_COMPOSE])
        assert {80, 443} <= published.get("nginx", set()), (
            "nginx must publish 80/443; it is the only application ingress"
        )

    def test_dev_override_publishes_app_on_8000(self) -> None:
        """The dev stack is where localhost:8000 is genuinely true."""
        override = COMPOSE_DIR / "docker-compose.override.yml"
        published = _host_ports([BASE_COMPOSE, override])
        assert 8000 in published.get("app", set()), (
            "the dev override must publish app:8000 -- that is the only compose "
            "variant in which http://localhost:8000 is a host address"
        )

    def test_readme_only_reaches_prometheus_from_inside_the_network(self) -> None:
        """Prometheus is expose-only in *every* variant, so :9090 is never a host address.

        ``localhost:8000`` stays allowed: the dev override publishes it, so it is a
        real host address for ``make dev`` / ``make dev-docker``. :9090 has no such
        variant, so it may only appear behind a ``compose exec``.
        """
        offenders = [
            line.strip()
            for line in _shell_lines(README.read_text(encoding="utf-8"))
            if "localhost:9090" in line and "exec" not in line
        ]
        assert not offenders, (
            "README advertises localhost:9090 as a host address, but prometheus uses "
            f"expose: only in every compose variant. Offending lines: {offenders}"
        )

    def test_readme_states_one_canonical_port_semantics(self) -> None:
        """README must separate the dev address from the production ingress once.

        ``localhost:8000`` stays documented, because it is genuinely true for
        ``make dev`` and for the dev compose override. What must not happen is the
        old single flat table that implied the same addresses for production --
        where ``app`` is not published and nginx owns 80/443.
        """
        text = README.read_text(encoding="utf-8")
        assert "localhost:8000" in text, (
            "README must keep documenting localhost:8000 -- it is the real address "
            "for `make dev` and the dev compose override"
        )
        assert "端口语义" in text, (
            "README must carry one canonical port-semantics statement separating the "
            "development address from the production ingress"
        )
        assert "NGINX_HTTP_PORT" in text, (
            "README must name the production ingress knob (NGINX_HTTP_PORT / "
            "NGINX_HTTPS_PORT -> 80/443); that is the only application port the "
            "production stack publishes"
        )

    @pytest.mark.parametrize(
        "port,service",
        sorted(CONTAINER_ONLY_PORTS.items(), key=lambda kv: int(kv[0])),
    )
    def test_ci_deploy_smoke_reaches_the_port_from_inside_the_network(
        self, port: str, service: str
    ) -> None:
        """The CI deploy job runs the real production stack, where the port is not published."""
        offenders = [
            line.strip()
            for line in _ci_deploy_script_lines()
            if f"localhost:{port}" in line and "exec" not in line
        ]
        assert not offenders, (
            f"CI deploy smoke curls localhost:{port} on the host, but {service} is "
            f"expose-only in the production stack, so the probe can never succeed: "
            f"{offenders}"
        )

    @pytest.mark.parametrize(
        "port,service",
        sorted(CONTAINER_ONLY_PORTS.items(), key=lambda kv: int(kv[0])),
    )
    def test_production_guide_reaches_the_port_from_inside_the_network(
        self, port: str, service: str
    ) -> None:
        """A container-local port is only meaningful behind a `compose exec`."""
        offenders = [
            line.strip()
            for line in _shell_lines(PROD_GUIDE.read_text(encoding="utf-8"))
            if f"localhost:{port}" in line and "exec" not in line
        ]
        assert not offenders, (
            f"production guide addresses localhost:{port} as if it were a host port, "
            f"but {service} is expose-only in the production stack: {offenders}"
        )

    def test_production_guide_states_the_canonical_port_semantics(self) -> None:
        assert "端口语义" in PROD_GUIDE.read_text(encoding="utf-8"), (
            "the production guide must state the port semantics once, so the ~20 "
            "individual probes do not each re-derive them"
        )

    def test_readme_distinguishes_dev_and_production_addresses(self) -> None:
        text = README.read_text(encoding="utf-8")
        assert "localhost:8000" in text, (
            "README must keep documenting localhost:8000 -- it is the real address "
            "for `make dev` and the dev compose override"
        )
        assert "NGINX_HTTP_PORT" in text, (
            "README must name the production ingress knob (NGINX_HTTP_PORT / "
            "NGINX_HTTPS_PORT -> 80/443); that is the only application port the "
            "production stack publishes"
        )
