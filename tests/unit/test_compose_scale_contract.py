"""Contract tests for Compose horizontal scaling (Issue #58).

The defect
----------
``docker-compose.scale.yml`` declares ``deploy.replicas`` for ``app``, while the base
compose pinned ``container_name: customer-service-ai``. Compose forbids the two
together — a fixed container name has to be globally unique — so the stack failed at
config time, before anything was created::

    services.deploy.replicas: can't set container_name and app as container name
    must be unique: invalid compose project

That made ``make scale N=3`` unusable, and it also meant the multi-replica
prerequisites already documented in that file (postgres checkpoint, redis session,
redis thread lock) could never actually be exercised.

What these tests pin
--------------------
1. No service that is scaled declares ``container_name`` — in the base file **and**
   in the prod overlay, which re-declared it and would have silently reinstated the
   conflict (an overlay does not inherit the base's deletions).
2. The container-name contract is explicit: Compose generates
   ``<project>-<service>-<index>``.
3. Nothing in the repo addresses the app/worker containers by their former literal
   names, so removing the fixed names breaks no tooling.
4. ``docker compose config`` succeeds for the scale stack and for every overlay
   combination.
5. The **documented** limits stay honest: production HA / autoscaling / multi-replica
   stability remain NOT_VERIFIED, and the measured fact that nginx does not currently
   spread traffic across replicas is recorded rather than implied away.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
BASE = COMPOSE_DIR / "docker-compose.yml"
PROD = COMPOSE_DIR / "docker-compose.prod.yml"
SCALE = COMPOSE_DIR / "docker-compose.scale.yml"
NGINX_CONF = REPO_ROOT / "deploy" / "nginx" / "nginx.conf"
README = REPO_ROOT / "README.md"

#: Services the scale overlay actually multiplies. ``worker`` is included because
#: the scale file's own header requires scaling it in lockstep with app.
SCALABLE = ("app", "worker")

#: Overlay combinations that must all remain valid. `make prod` and `make scale`
#: both stack prod, so prod has to be included or the defect simply moves.
OVERLAY_COMBOS = {
    "base": [],
    "prod": ["-f", str(PROD)],
    "override": ["-f", str(COMPOSE_DIR / "docker-compose.override.yml")],
    "canary": ["-f", str(PROD), "-f", str(COMPOSE_DIR / "docker-compose.canary.yml")],
    "scale": ["-f", str(PROD), "-f", str(SCALE)],
    "monitoring": ["-f", str(PROD), "-f", str(COMPOSE_DIR / "docker-compose.monitoring.yml")],
}


#: Overlays that participate in a scaled stack. `docker-compose.override.yml` is
#: excluded on purpose: it is the dev stack, which publishes a host port and is
#: therefore single-instance by construction (see
#: ``test_dev_override_is_the_only_exemption_and_why``).
SCALING_OVERLAY_FILES = (
    PROD,
    SCALE,
    COMPOSE_DIR / "docker-compose.canary.yml",
    COMPOSE_DIR / "docker-compose.monitoring.yml",
)


def _services(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))["services"]


def _has_docker() -> bool:
    return shutil.which("docker") is not None


requires_docker = pytest.mark.skipif(not _has_docker(), reason="docker CLI not available")

#: Compose files whose `${VAR:?...}` fail-fast requirements must be satisfiable
#: for a render-only probe.
_COMPOSE_FILES_FOR_ENV = (
    BASE,
    PROD,
    SCALE,
    COMPOSE_DIR / "docker-compose.override.yml",
    COMPOSE_DIR / "docker-compose.canary.yml",
    COMPOSE_DIR / "docker-compose.monitoring.yml",
)

_REQUIRED_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?")


def _required_compose_vars() -> set[str]:
    """Every ``${VAR:?}`` the compose files declare.

    Derived from the files rather than hand-listed, so a new fail-fast
    requirement (``#47`` added ``ADMIN_PASSWORD``, ``#48``/``#57`` added more)
    cannot make this probe silently depend on a developer's ambient ``.env``.
    Deriving it keeps the render hermetic *and* keeps it honest: nothing here
    has to be kept in sync by hand.
    """
    names: set[str] = set()
    for path in _COMPOSE_FILES_FOR_ENV:
        if not path.is_file():
            continue
        names.update(_REQUIRED_VAR_RE.findall(path.read_text(encoding="utf-8")))
    return names


def _compose_config(*files: str, project: str = "issue58") -> subprocess.CompletedProcess:
    """Render the merged stack in a hermetic env (no project .env, no host state)."""
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/tmp",
    }
    env.update({name: "probe_not_a_real_secret" for name in _required_compose_vars()})
    args = [
        "docker",
        "compose",
        "--project-directory",
        str(COMPOSE_DIR),
        "-f",
        str(BASE),
        *files,
        "--env-file",
        "/dev/null",
        "config",
    ]
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=300, check=False)


# ---------------------------------------------------------------------------
# A. the fix itself: no fixed container name on a scaled service
# ---------------------------------------------------------------------------


class TestScaledServicesHaveNoFixedContainerName:
    @pytest.mark.unit
    @pytest.mark.parametrize("service", SCALABLE)
    def test_base_compose_declares_no_container_name(self, service: str) -> None:
        svc = _services(BASE).get(service)
        assert svc is not None, f"base compose lost the {service!r} service"
        assert "container_name" not in svc, (
            f"{service} declares container_name, which Compose forbids alongside "
            "deploy.replicas / --scale; that is exactly the #58 config error"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("service", SCALABLE)
    def test_prod_overlay_does_not_reintroduce_it(self, service: str) -> None:
        """The overlay is the layer `make prod` and `make scale` both stack.

        Deleting the key in the base file is not enough: an overlay that re-declares
        it puts the conflict straight back, and it would only surface for the
        prod-based commands.
        """
        svc = _services(PROD).get(service)
        if svc is None:
            pytest.skip(f"prod overlay does not redefine {service!r}")
        assert "container_name" not in svc, (
            f"the prod overlay re-declares container_name for {service}; overrides do "
            "not inherit deletions from the base file, so #58 would return for "
            "`make prod` / `make scale`"
        )

    @pytest.mark.unit
    def test_no_scaling_overlay_reintroduces_it_for_scaled_services(self) -> None:
        """Every overlay that participates in a scaled stack must stay clean.

        The dev override is deliberately exempt and asserted separately: it publishes
        ``${APP_PORT:-8000}:8000`` to the host, and two replicas cannot both bind that
        port, so the dev stack is single-instance by construction and a stable name
        there is correct rather than an oversight.
        """
        offenders: list[str] = []
        for overlay in SCALING_OVERLAY_FILES:
            services = _services(overlay)
            for service in SCALABLE:
                svc = services.get(service)
                if svc and "container_name" in svc:
                    offenders.append(f"{overlay.name}:{service}")
        assert not offenders, f"container_name reintroduced by an overlay: {offenders}"

    def test_dev_override_is_the_only_exemption_and_why(self) -> None:
        override = COMPOSE_DIR / "docker-compose.override.yml"
        svc = _services(override)["app"]
        assert "container_name" in svc, (
            "the dev override is expected to keep a fixed name; if that changes, this "
            "test needs revisiting rather than being quietly relaxed"
        )
        assert [p for p in (svc.get("ports") or []) if ":" in str(p)], (
            "the exemption rests on the dev stack publishing a host port, which makes it "
            "single-instance; if the port mapping goes, the exemption must go with it"
        )

    @pytest.mark.unit
    def test_scale_overlay_actually_declares_replicas(self) -> None:
        replicas = _services(SCALE).get("app", {}).get("deploy", {}).get("replicas")
        assert replicas, (
            "the scale overlay must declare replicas for app, otherwise removing "
            "container_name buys nothing"
        )
        assert int(replicas) >= 2, f"replicas={replicas} is not horizontal scaling"

    @pytest.mark.unit
    def test_unscaled_services_keep_their_stable_names(self) -> None:
        """Only the scalable services lose their fixed name.

        Dropping container_name everywhere would churn the operational surface for
        no benefit: these are never multiplied.
        """
        services = _services(BASE)
        for name in ("postgres", "redis", "qdrant"):
            assert "container_name" in services[name], (
                f"{name} should keep its fixed container_name; it is never scaled and a "
                "stable name is useful to operators"
            )


# ---------------------------------------------------------------------------
# B. the naming contract, and that nothing depended on the old names
# ---------------------------------------------------------------------------


class TestContainerNamingContract:
    @pytest.mark.unit
    def test_naming_is_delegated_to_compose(self) -> None:
        """With no container_name, Compose guarantees <project>-<service>-<index>."""
        for service in SCALABLE:
            assert "container_name" not in _services(BASE)[service]
        # the mechanism is Compose's own; nothing in the repo should hardcode the
        # generated shape in a way that would break if the project name changes
        text = BASE.read_text(encoding="utf-8")
        assert not re.search(r"container_name:\s*customer-service-(ai|worker)-\d", text), (
            "do not hardcode an index-suffixed name; that reimplements Compose's naming"
        )

    @pytest.mark.unit
    def test_no_tooling_addresses_the_former_container_names(self) -> None:
        """Removing the fixed names is only safe if nothing addressed them.

        Service-scoped commands (``docker compose logs app``) keep working under
        replicas. What breaks is addressing a *container* by its old literal name --
        ``docker logs <former-name>`` would target nothing once Compose owns the
        naming. So this matches that shape specifically rather than the bare
        substring, which also occurs in the repository name
        (``customer-service-ai-agent``) and in monitoring labels that have nothing
        to do with container identity.

        This file is excluded from its own scan. A guard has to name the shape it
        forbids, so leaving the guard in scope only proves that a guard matches its
        own docstring -- the failure this test was actually reporting before the
        exclusion. Nothing else is exempt.
        """
        pattern = re.compile(
            r"docker\s+(?:exec|logs|inspect|restart|stop|rm|cp|kill|attach|port)\s+"
            r"(?:-\S+\s+)*customer-service-(?:ai|worker)\b"
        )
        this_file = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
        offenders: list[tuple[str, str]] = []
        for rel in subprocess.run(
            ["git", "ls-files"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.splitlines():
            if rel == this_file:
                continue
            path = REPO_ROOT / rel
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            offenders.extend((rel, m.group(0)) for m in pattern.finditer(content))
        assert not offenders, (
            "these address the app/worker container by its removed fixed name; under "
            f"replicas they would silently target one replica or nothing: {offenders}"
        )
        # Non-vacuity: the excluded file must actually match, or the exclusion is
        # hiding a broken pattern rather than a self-reference.
        assert pattern.search(
            "docker logs customer-service-ai"
        ), "guard pattern no longer matches the shape it is meant to forbid"

    @pytest.mark.unit
    def test_monitoring_labels_are_static_not_container_derived(self) -> None:
        """`service: 'customer-service-ai'` looks like a coupling but is not.

        It is a hardcoded label attached to `targets: ['app:8000']`, so it does not
        read the container name. Guarding it documents that, so nobody "fixes" it
        later by chasing a non-existent dependency.
        """
        prom = (REPO_ROOT / "monitoring" / "prometheus.yml").read_text(encoding="utf-8")
        assert "service: 'customer-service-ai'" in prom
        assert "targets: ['app:8000']" in prom, (
            "the label must stay attached to the service-name target, which is what "
            "keeps it independent of the container name"
        )


# ---------------------------------------------------------------------------
# C. the config actually renders
# ---------------------------------------------------------------------------


@requires_docker
class TestComposeConfigSucceeds:
    @pytest.mark.unit
    @pytest.mark.parametrize("name", sorted(OVERLAY_COMBOS), ids=sorted(OVERLAY_COMBOS))
    def test_overlay_combination_renders(self, name: str) -> None:
        result = _compose_config(*OVERLAY_COMBOS[name])
        assert result.returncode == 0, f"{name} stack failed to render:\n{result.stderr[-1200:]}"

    @pytest.mark.unit
    def test_scale_stack_renders_replicas(self) -> None:
        import yaml

        result = _compose_config("-f", str(PROD), "-f", str(SCALE))
        assert result.returncode == 0, result.stderr[-1200:]
        rendered = yaml.safe_load(result.stdout)["services"]
        assert rendered["app"].get("deploy", {}).get("replicas"), (
            "the scale stack must actually carry replicas through to the rendered config"
        )
        assert "container_name" not in rendered["app"], (
            "the rendered app service must not carry a fixed name, or Compose would "
            "have refused the merge anyway"
        )

    @pytest.mark.unit
    def test_no_rendered_service_pairs_replicas_with_a_fixed_name(self) -> None:
        """The invariant that produced the original error, checked on rendered output."""
        import yaml

        for name, files in OVERLAY_COMBOS.items():
            result = _compose_config(*files)
            assert result.returncode == 0, f"{name}: {result.stderr[-600:]}"
            services = yaml.safe_load(result.stdout)["services"]
            for svc_name, svc in services.items():
                replicas = (svc.get("deploy") or {}).get("replicas") or 0
                if replicas and svc.get("container_name"):
                    pytest.fail(
                        f"{name}/{svc_name} renders replicas={replicas} together with "
                        f"container_name={svc['container_name']!r} — the #58 error"
                    )


# ---------------------------------------------------------------------------
# D. the upstream contract, and what it does NOT give us
# ---------------------------------------------------------------------------


class TestNginxUpstreamContract:
    @pytest.mark.unit
    def test_upstream_names_the_service_not_a_container(self) -> None:
        """`app` is the Compose service name; that is what survives replicas."""
        conf = NGINX_CONF.read_text(encoding="utf-8")
        upstreams = re.findall(r"upstream\s+(\w+)\s*\{([^}]*)\}", conf)
        assert upstreams, "the reverse proxy must still declare its upstreams"
        targets = [t for _, body in upstreams for t in re.findall(r"server\s+(\S+);", body)]
        assert targets, "upstream blocks must name at least one server"
        for target in targets:
            host = target.rsplit(":", 1)[0]
            assert host in {"app", "canary"}, (
                f"upstream target {target!r} is neither a Compose service name nor a "
                "port; a container name would not resolve under replicas"
            )

    @pytest.mark.unit
    def test_upstream_uses_a_single_server_entry_per_backend(self) -> None:
        """Documents the limitation instead of leaving it to be discovered.

        nginx resolves an upstream hostname once, at startup. Docker's embedded DNS
        answers one address per query, so a single `server app:8000` yields exactly
        one backend — measured with three replicas: nginx started fine and every
        request went to the same replica.

        If someone later adds a `resolver`/variable `proxy_pass`, this test is the
        signal to also revisit the documentation claims.
        """
        conf = NGINX_CONF.read_text(encoding="utf-8")
        for name, body in re.findall(r"upstream\s+(\w+)\s*\{([^}]*)\}", conf):
            servers = re.findall(r"server\s+(\S+);", body)
            assert len(servers) == 1, (
                f"upstream {name} now lists {len(servers)} servers; if this is a "
                "deliberate move to real distribution, the NOT_VERIFIED statements "
                "about multi-replica behaviour must be revisited at the same time"
            )

    @pytest.mark.unit
    def test_scale_file_records_the_measured_distribution_gap(self) -> None:
        text = SCALE.read_text(encoding="utf-8")
        assert "不会在 nginx 前均匀分摊" in text, (
            "the scale file must record that replicas are not currently spread by "
            "nginx; an operator reading only the file would otherwise assume they are"
        )
        assert "NOT_VERIFIED" in text


# ---------------------------------------------------------------------------
# E. the honesty guard: production HA stays unproven
# ---------------------------------------------------------------------------


class TestProductionHaRemainsUnverified:
    @pytest.mark.unit
    def test_readme_still_marks_multi_replica_and_autoscaling_unverified(self) -> None:
        readme = README.read_text(encoding="utf-8")
        assert "PRODUCTION NOT_VERIFIED" in readme, (
            "the README must keep its PRODUCTION NOT_VERIFIED banner; making the scale "
            "stack configurable must not read as making it production-proven"
        )
        for phrase in ("多副本长期稳定", "autoscaling"):
            assert phrase in readme, (
                f"the NOT_VERIFIED list must still cover {phrase!r} — Compose scaling "
                "being configurable is not the same as having been run in production"
            )

    @pytest.mark.unit
    def test_no_document_claims_proven_horizontal_scaling(self) -> None:
        """Guard against a 'now supports HA / autoscaling' claim sneaking in."""
        banned = re.compile(
            r"(已验证|验证通过|生产已验证|production[- ]verified).{0,24}"
            r"(水平扩展|横向扩展|horizontal scal|autoscal|高可用|HA)",
            re.IGNORECASE,
        )
        offenders: list[str] = []
        for rel in ("README.md", "docs/reference/current-state.md"):
            path = REPO_ROOT / rel
            if not path.is_file():
                continue
            for match in banned.finditer(path.read_text(encoding="utf-8")):
                offenders.append(f"{rel}: {match.group(0)[:60]!r}")
        assert not offenders, f"unsupported HA/scaling claim(s): {offenders}"

    @pytest.mark.unit
    def test_scale_file_does_not_claim_throughput_numbers(self) -> None:
        text = SCALE.read_text(encoding="utf-8")
        assert not re.search(r"\d+\s*(QPS|qps|req/s|rps)", text), (
            "the scale file must not carry throughput numbers; none were measured"
        )
