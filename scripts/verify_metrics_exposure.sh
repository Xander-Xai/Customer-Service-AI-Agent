#!/usr/bin/env bash
# 真实 Prometheus 端到端验证：DLQ 告警是否真的会触发。
#
# 它验证什么
# ----------
# 1. `GET /metrics` 真的把 `prometheus_client` 注册表暴露出来（target 必须 UP）；
# 2. **另一个进程**（此处模拟 Celery worker 容器）递增的
#    `agent_run_dead_letter_total`，经多进程聚合后**对抓取侧可见**；
# 3. `monitoring/alert_rules.yml` 里的真实规则在该时间序列上**进入 FIRING**。
#
# 第 2 条是容易被漏掉的一半：只补暴露端点而不做跨进程聚合，指标会「存在、target 是
# UP、值恒为 0」，告警仍然不触发，而且比「指标不存在」更难发现。
#
# 边界（不得越界宣称）
# --------------------
# 本脚本**不是**整套 docker compose 栈的验收：
# - 暴露侧复用的是真实路由函数 `api.routes.monitoring._registry_exposition`，
#   但**没有**启动完整 FastAPI app / Celery worker 二进制；
# - 未验证 Alertmanager 的实际通知投递（webhook/SMTP），也未验证 Grafana 渲染；
# - 「整套部署的 DLQ 告警闭环已验证」仍属 NOT_VERIFIED，见 docs/limitations.md。
#
# 结论口径：本脚本 PASS ⟹ 指标暴露链路与告警表达式在真实 Prometheus 上成立。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROM_IMAGE="${PROM_IMAGE:-prom/prometheus:v2.51.0}"
WORKDIR="$(mktemp -d)"
APP_PORT="${APP_PORT:-9100}"
STATE="${WORKDIR}/state.json"

cleanup() {
  [[ -n "${APP_PID:-}" ]] && kill "$APP_PID" 2>/dev/null || true
  [[ -n "${WRITER_PID:-}" ]] && kill "$WRITER_PID" 2>/dev/null || true
  docker rm -f csai-promverify-prom >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> 1/6 准备隔离的多进程指标目录与暴露/写入两个进程"
mkdir -p "${WORKDIR}/mp"

cat > "${WORKDIR}/writer.py" <<'PY'
"""Stand-in for the Celery worker container: only *writes* runtime counters."""
import os, sys, time
sys.path.insert(0, os.environ["REPO_ROOT"])
from core.monitoring import agent_run_dead_letter_total
time.sleep(float(os.environ["WRITER_DELAY_SECONDS"]))
for _ in range(int(os.environ["DEAD_LETTERS"])):
    agent_run_dead_letter_total.inc()
print("worker: incremented agent_run_dead_letter_total", flush=True)
time.sleep(3600)
PY

cat > "${WORKDIR}/exposition_app.py" <<'PY'
"""Stand-in for the app container: the ONLY process Prometheus scrapes.

Uses the real route helper so this verifies the shipped exposition code path.
"""
import os, sys
sys.path.insert(0, os.environ["REPO_ROOT"])
import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from api.routes.monitoring import _registry_exposition

app = FastAPI()


@app.get("/metrics")
async def metrics() -> Response:
    return Response(
        content=_registry_exposition(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


uvicorn.run(app, host="127.0.0.1", port=int(os.environ["APP_PORT"]), log_level="warning")
PY

export REPO_ROOT WORKDIR APP_PORT
export SERVICE_ROLE=worker API_KEY_ENABLED=false
export DEAD_LETTERS="${DEAD_LETTERS:-3}"
export WRITER_DELAY_SECONDS="${WRITER_DELAY_SECONDS:-60}"

cat > "${WORKDIR}/prom.yml" <<YML
global:
  scrape_interval: 5s
  evaluation_interval: 5s
rule_files:
  - /etc/prometheus/rules/alert_rules.yml
scrape_configs:
  - job_name: 'csai-registry'
    metrics_path: '/metrics'
    static_configs:
      - targets: ['127.0.0.1:${APP_PORT}']
YML

PROMETHEUS_MULTIPROC_DIR="${WORKDIR}/mp" python3 -m core.metrics_exposition >/dev/null 2>&1 || true

PROMETHEUS_MULTIPROC_DIR="${WORKDIR}/mp" python3 "${WORKDIR}/exposition_app.py" &
APP_PID=$!
PROMETHEUS_MULTIPROC_DIR="${WORKDIR}/mp" python3 "${WORKDIR}/writer.py" &
WRITER_PID=$!

echo "==> 2/6 启动真实 Prometheus（${PROM_IMAGE}）"
docker rm -f csai-promverify-prom >/dev/null 2>&1 || true
docker run -d --name csai-promverify-prom --network host \
  -v "${WORKDIR}/prom.yml:/etc/prometheus/prometheus.yml:ro" \
  -v "${REPO_ROOT}/monitoring/alert_rules.yml:/etc/prometheus/rules/alert_rules.yml:ro" \
  "${PROM_IMAGE}" \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path="${WORKDIR}/tsdb" >/dev/null

echo "==> 3/6 等待 target UP 并确认基线样本存在（值应为 0）"
for _ in $(seq 1 30); do
  health="$(curl -s "http://127.0.0.1:9090/api/v1/targets" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['activeTargets'][0]['health'])" 2>/dev/null || echo unknown)"
  [[ "$health" == "up" ]] && break
  sleep 2
done
[[ "$health" == "up" ]] || { echo "❌ Prometheus target 未 UP（当前 ${health}）"; exit 1; }
echo "    target=UP"

baseline="$(curl -s "http://127.0.0.1:9090/api/v1/query?query=agent_run_dead_letter_total" \
  | python3 -c "import sys,json;r=json.load(sys.stdin)['data']['result'];print(r[0]['value'][1] if r else 'MISSING')")"
echo "    基线 agent_run_dead_letter_total=${baseline}"
[[ "$baseline" == "MISSING" ]] && { echo "❌ DLQ 时间序列不存在于 Prometheus"; exit 1; }

echo "==> 4/6 等待 worker 进程写入 + 告警进入 FIRING（for: 1m）"
fired=""
for _ in $(seq 1 60); do
  fired="$(curl -s "http://127.0.0.1:9090/api/v1/rules" \
    | python3 -c "
import sys, json
for g in json.load(sys.stdin)['data']['groups']:
    for r in g['rules']:
        if r.get('name') == 'AgentRunDeadLetterDetected' and r['state'] == 'firing':
            print('firing')
" 2>/dev/null || true)"
  [[ "$fired" == "firing" ]] && break
  sleep 5
done
[[ "$fired" == "firing" ]] || { echo "❌ 告警未进入 FIRING"; exit 1; }

echo "==> 5/6 记录证据"
python3 - "$REPO_ROOT" "$WORKDIR" "$baseline" "$DEAD_LETTERS" <<'PY'
import datetime, json, pathlib, sys, urllib.request

repo, workdir, baseline, dead_letters = sys.argv[1:5]
sys.path.insert(0, repo)

from core.code_provenance import collect_code_provenance  # noqa: E402


def get(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


alerts = [
    a
    for a in get("http://127.0.0.1:9090/api/v1/alerts")["data"]["alerts"]
    if a["labels"]["alertname"] == "AgentRunDeadLetterDetected"
]
rule = next(
    r
    for g in get("http://127.0.0.1:9090/api/v1/rules")["data"]["groups"]
    for r in g["rules"]
    if r.get("name") == "AgentRunDeadLetterDetected"
)
final_value = get(
    "http://127.0.0.1:9090/api/v1/query?query=agent_run_dead_letter_total"
)["data"]["result"][0]["value"][1]

provenance = collect_code_provenance(repo)

report = {
    "schema_version": "metrics-exposure-evidence/v1",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "tested_code_sha": provenance.commit_sha,
    "code_provenance": provenance.to_dict(),
    "overall_status": "VERIFIED_LOCAL",
    "scope": {
        "verifies": [
            "GET /metrics serves the real prometheus_client registry",
            "counters written by a SEPARATE process (Celery worker stand-in) are "
            "visible to the scraper via PROMETHEUS_MULTIPROC_DIR aggregation",
            "monitoring/alert_rules.yml::AgentRunDeadLetterDetected reaches FIRING "
            "against a real Prometheus v2.51.0",
        ],
        "does_not_verify": [
            "full docker compose stack (real FastAPI app + real Celery worker binary)",
            "Alertmanager notification delivery (webhook / SMTP)",
            "Grafana panel rendering in a live Grafana",
            "production cluster / real traffic",
        ],
        "status_after_real_run": "VERIFIED_LOCAL",
        "production_status": "NOT_VERIFIED",
    },
    "evidence": {
        "prometheus_target_health": "up",
        "baseline_value": float(baseline),
        "final_value": float(final_value),
        "dead_letters_injected_by_worker": int(dead_letters),
        "alert_state": rule["state"],
        "alert_rule_last_error": rule.get("lastError") or None,
        "alert_annotations": (alerts[0].get("annotations") if alerts else None),
    },
}

out_dir = pathlib.Path(repo) / "artifacts" / "observability"
out_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = out_dir / f"metrics-exposure-{stamp}"
out.mkdir(parents=True, exist_ok=True)
(out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"    evidence -> {out.relative_to(repo)}/report.json")
PY

echo "==> 6/6 结论"
echo "✅ 真实 Prometheus 端到端 PASS："
echo "   - /metrics 暴露了真实注册表（target UP）"
echo "   - worker 进程写入的 agent_run_dead_letter_total 对抓取侧可见（跨进程聚合生效）"
echo "   - AgentRunDeadLetterDetected 实际进入 FIRING"
echo "   边界：整套 compose 栈 / Alertmanager 通知 / 生产集群仍 NOT_VERIFIED"