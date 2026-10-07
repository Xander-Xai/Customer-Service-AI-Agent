# Reproducible offline demo

> **Purpose** — run a single, deterministic, network-free demonstration of the
> customer-service routing path and receive an auditable proof card. Built for
> open-source users, reviewers and maintainers; it needs no API key, no database
> and no external service.

Lifecycle: 🟠 RUNBOOK · Evidence level: `LOCAL_VERIFIED` (offline, Mock LLM) + expected `CI_VERIFIED`.

## What it is

```bash
make demo-offline
```

The demo executes the canonical routing end-to-end test anchors through the
existing graph orchestration and the existing MockLLM / local-embedding
fixtures. It does **not** add an agent, rebuild the frontend, or call a model.

Properties (each is asserted, not merely claimed):

| Property | How it holds |
|---|---|
| **No API key** | Runs the offline lane (`EMBEDDING/RERANKER/STT/TTS provider=local` from `.env.test`). |
| **No internet** | `scripts/offline_egress_guard.py` is installed as a pytest plugin; any real HTTP/DNS/socket egress raises immediately. Only loopback and UNIX sockets are allowed. |
| **Deterministic** | MockLLM + deterministic local providers + fixed test fixtures; no randomness, no wall-clock dependence. |
| **Honest verdict** | The verdict is derived from the real pytest process exit status. `0 → PASS`, `5 (no tests collected) → NOT_RUN`, anything else → `FAIL`. A non-run or a failed run is **never** reported as `PASS`. |

## Proof card

The run prints a card and writes the same JSON to
`artifacts/demo/offline-<UTC timestamp>.json` (gitignored):

```text
DEMO PROOF CARD
  Scenario
  Mode            MOCK/OFFLINE
  Verdict         PASS | FAIL | NOT_RUN
  Git SHA
  UTC timestamp
  Test command
  Source anchors
  Test anchors
  Environment limitations
```

The command exits non-zero unless the verdict is `PASS`.

## Run it

```bash
# 1) install once
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 2) run (uses the offline lane from .env.test; `make` switches the env for you)
make demo-offline
```

Without `make` (e.g. Windows PowerShell / Git Bash):

```bash
cp .env.test .env
python3 scripts/demo_offline.py
```

Override the exercised nodes (repeatable) or the artifact path:

```bash
python3 scripts/demo_offline.py --node tests/integration/test_integration.py::TestRoutingLogic
python3 scripts/demo_offline.py --json-out /tmp/demo-card.json
```

Run everything from the repository root; all paths are repository-relative.

## Source and test anchors

| Concern | Location |
|---|---|
| Graph construction | `core/graph_builder.py::build_graph` |
| Dual-layer routing (LLM ∥ rule) | `router/query_router.py` (`asyncio.gather(llm, rule)`) |
| Collaboration modes | `collaboration/modes.py` (5 `CollaborationMode` subclasses) |
| Egress guard | `scripts/offline_egress_guard.py` |
| Demo driver + proof card | `scripts/demo_offline.py` |
| Contract tests | `tests/unit/test_demo_offline_contract.py` |

The demo runs, by default:

- `tests/integration/test_integration.py::TestGraphEndToEnd::test_simple_query_end_to_end`
- `tests/integration/test_integration.py::TestGraphEndToEnd::test_cache_hit_skips_routing`
- `tests/integration/test_integration.py::TestRoutingLogic`

## Limitations (not covered by this demo)

- It is **Mock LLM + local embedding**, not a real provider call. Real-provider
  end-to-end is `NOT_RUN` here (needs credentials).
- The durable distributed-runtime demos (`make runtime-e2e`, `make runtime-chaos`,
  `make runtime-verify`) need real PostgreSQL + Redis and are **out of scope**.
- Real ERP writes (`NOT_VERIFIED`), production cluster / multi-replica / QPS /
  P95 (`NOT_VERIFIED`) are not exercised.
- Deferred evidence is tracked in [Issue #7](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/7).

## Related

- Distributed-runtime acceptance: `docs/operations/distributed-runtime-runbook.md`
- Evidence boundary: `docs/evaluation/production-evidence.md`
