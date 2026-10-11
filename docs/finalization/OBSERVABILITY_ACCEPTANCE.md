# Observability Acceptance Report

> Verification executed for this finalization. Anything not actually executed is
> labelled **NOT_VERIFIED** — not described as complete.
> Sources: `core/monitoring.py`, `core/metrics_exposition.py`,
> `api/routes/monitoring.py`, `monitoring/`, `scripts/verify_metrics_exposure.sh`.

---

## 1. Actual verification results

| Check | Command | Result | Evidence |
|---|---|---|---|
| Metric exposure contract | `make metrics-exposure-check` | **PASS** — 21 tests | `tests/unit/test_metrics_exposure_contract.py`, `test_metrics_multiprocess.py`, `test_compose_metrics_topology.py` |
| Prometheus config + rule unit tests | `make alert-rules-test` | **PASS** — `promtool check config` (5 rules) + `promtool test rules` SUCCESS | `monitoring/alert_rules.yml`, `alert_rules_test.yml` |
| Real Prometheus end-to-end, DLQ alert | `make metrics-exposure-verify` | **PASS** — target UP; worker-incremented `agent_run_dead_letter_total` visible to the scraper; `AgentRunDeadLetterDetected` reaches **FIRING** | `artifacts/observability/metrics-exposure-<ts>/report.json` |

### What the end-to-end check actually proves

`scripts/verify_metrics_exposure.sh` runs **real** `prom/prometheus:v2.51.0`,
scrapes a real exposition process, has a **separate worker process** increment
`agent_run_dead_letter_total` three times via `PROMETHEUS_MULTIPROC_DIR` +
`MultiProcessCollector`, and observes the alert transition to `firing`. This
proves:

1. `GET /metrics` serialises the real `REGISTRY` (not a stub);
2. cross-process aggregation works (the app-scrape sees worker-written samples);
3. the DLQ alert expression is satisfiable on real Prometheus — it is not a
   permanently-unfirable rule.

---

## 2. The chain, link by link

| Link | State | Note |
|---|---|---|
| ~70 metrics registered | **Implemented** | `core/monitoring.py` |
| Registry serialised | **Implemented / VERIFIED_LOCAL** | `GET /metrics` → `generate_latest(REGISTRY)`; `api/routes/monitoring.py::_registry_exposition`; `/metrics/prometheus` keeps `csai_*` business aggregates |
| Cross-process visibility | **Implemented / VERIFIED_LOCAL** | `PROMETHEUS_MULTIPROC_DIR` + `MultiProcessCollector`; `core/metrics_exposition.py`; gauge modes explicitly declared |
| Scrape authentication | **Implemented / VERIFIED_LOCAL** | `bearer_token_file` on the Prometheus side; `Authorization: Bearer` accepted in `api/utils.py::check_admin_token` |
| DLQ alert expression | **VERIFIED_LOCAL** | `increase(agent_run_dead_letter_total[5m]) > 0` firing on real Prometheus + `promtool test rules` |
| **Alertmanager notification delivery** | **NOT_VERIFIED** | no webhook/SMTP receiver exercised end-to-end |
| **Full compose stack (real FastAPI + real Celery binary + Grafana render)** | **NOT_VERIFIED** | the e2e check uses the real exposition functions in a harness, not the whole stack |
| **Production cluster / multi-replica long run** | **NOT_VERIFIED** | no production evidence |
| Grafana dashboards | **Implemented, NOT_VERIFIED rendering** | 6 previously-empty panels now backed by real series (contract test); never rendered in a live Grafana |

Correct summary: **"metrics are reachable and the alert expression holds on real
Prometheus" is verified; "a human actually received a notification" is not.**
Do not call this a complete alerting closure.

---

## 3. Regression guards (so the chain cannot silently break again)

`tests/unit/test_metrics_exposure_contract.py` asserts that **every metric name
referenced by an alert rule or a Grafana panel** appears in the exposition
surface. This converts "we registered it but never exposed it" from a silent
production bug into a red test. `test_metrics_multiprocess.py` pins the
multi-process aggregation semantics; `test_compose_metrics_topology.py` pins the
shared volume / scrape topology.

---

## 4. Reproduce

```bash
make metrics-exposure-check     # contract: every referenced metric is reachable
make monitoring-token           # fail-closed scrape credential
make alert-rules-test           # promtool: syntax + will-fire + no latching
make metrics-exposure-verify    # real Prometheus -> DLQ alert FIRING
```

To close the remaining gap: point Alertmanager at a real receiver and run one
firing→notification round-trip, then record the artifact. Until then the
notification link stays **NOT_VERIFIED**.
