#!/usr/bin/env bash
#
# Reproducible pipeline for the Explanation Service with an Audit Trail.
#
# Stages:
#   1. install      - install Python dependencies
#   2. generate     - generate deterministic monthly Parquet batches (data/generator.py)
#   3. init-iceberg - (optional) initialize Apache Iceberg tables + load batches
#   4. test         - run Person 2 (pytest) and Person 3 (unittest) test suites
#   5. serve        - start the FastAPI service in the background
#   6. demo         - exercise the /explain, /decision, /replay, /consistency endpoints
#   7. benchmark    - run p50/p95 latency benchmarks
#   8. compliance   - generate / show the Person 3 compliance report
#
# Usage:
#   ./scripts/pipeline.sh [stage]       # run a single stage
#   ./scripts/pipeline.sh all           # run the full pipeline (1,2,4,5,6,7)
#   ./scripts/pipeline.sh help
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BACKEND="${EXPLANATION_REPO_BACKEND:-duckdb}"
SERVICE_URL="http://127.0.0.1:8000"
LOG_DIR="${ROOT}/.pipeline_logs"
PID_FILE="${LOG_DIR}/service.pid"

mkdir -p "${LOG_DIR}"

log() { echo "[pipeline] $*"; }

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "ERROR: required command not found: $1"
    exit 1
  fi
}

stage_install() {
  log "Stage: install — Python dependencies"
  require_cmd python3
  python3 -m pip install --upgrade pip
  python3 -m pip install -r requirements.txt
  log "Install complete."
}

stage_generate() {
  log "Stage: generate — deterministic monthly Parquet batches"
  python3 person2_query_interface/data/generator.py
  log "Generate complete."
}

stage_init_iceberg() {
  log "Stage: init-iceberg — Apache Iceberg warehouse + load batches"
  python3 init_iceberg.py
  log "Init-iceberg complete."
}

stage_test() {
  log "Stage: test — test suites"
  log "Person 2: pytest (DuckDB backend)"
  EXPLANATION_REPO_BACKEND=duckdb python3 -m pytest person2_query_interface/tests -v \
    | tee "${LOG_DIR}/person2_tests.log"
  log "Person 3: unittest compliance module"
  (cd person3_compliance_audit && python3 -m unittest discover -s tests -v \
    | tee "${LOG_DIR}/person3_tests.log")
  log "Test stage complete."
}

wait_for_service() {
  local tries=60
  while (( tries-- > 0 )); do
    if curl -sf "${SERVICE_URL}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  log "ERROR: service did not become healthy at ${SERVICE_URL}"
  cat "${LOG_DIR}/service.log" 2>/dev/null || true
  return 1
}

stage_serve() {
  log "Stage: serve — FastAPI service (backend=${BACKEND})"
  if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
    log "Service already running (PID $(cat "${PID_FILE}"))."
    return 0
  fi
  EXPLANATION_REPO_BACKEND="${BACKEND}" \
    nohup uvicorn person2_query_interface.app.service:app \
      --host 127.0.0.1 --port 8000 \
      >"${LOG_DIR}/service.log" 2>&1 &
  echo $! > "${PID_FILE}"
  log "Service started (PID $(cat "${PID_FILE}")). Waiting for health..."
  wait_for_service
  curl -s "${SERVICE_URL}/health" | python3 -m json.tool
  log "Serve stage complete."
}

stage_demo() {
  log "Stage: demo — exercise query interface endpoints"
  require_cmd curl
  require_cmd jq

  # Pick a prediction_id from the repository
  local pred_id
  pred_id=$(python3 - <<'PY'
from person2_query_interface.app.repository import get_repository
repo = get_repository()
ids = repo.list_prediction_ids()
print(ids[0] if ids else "")
PY
)
  if [[ -z "${pred_id}" ]]; then
    log "ERROR: no prediction ids available; run 'generate' first."
    exit 1
  fi
  log "Using prediction_id=${pred_id}"

  # Derive its request_trace_id and a time window for /explain
  read -r trace_id from_ts to_ts <<< "$(python3 - <<PY
from person2_query_interface.app.repository import get_repository
repo = get_repository()
bundle = repo.get_decision_bundle("${pred_id}")
print(bundle.prediction.request_trace_id, bundle.prediction.decision_ts, bundle.prediction.decision_ts)
PY
)"

  log "GET /explain"
  curl -s "${SERVICE_URL}/explain?request_trace_id=${trace_id}&from_ts=${from_ts}&to_ts=${to_ts}" \
    | jq '{query, "observability.latency_ms": .observability.latency_ms, "within_slo": .observability.within_slo}' \
    | tee "${LOG_DIR}/demo_explain.json"

  log "GET /decision/${pred_id}"
  curl -s "${SERVICE_URL}/decision/${pred_id}" \
    | jq '{prediction_id: .decision.prediction.prediction_id, demo_decision: .decision.prediction.demo_decision, risk_score: .decision.prediction.risk_score, model_version: .decision.prediction.model_version, explainer: (.decision.explanation.explainer_name + " " + .decision.explanation.explainer_version)}' \
    | tee "${LOG_DIR}/demo_decision.json"

  log "GET /replay/${pred_id}"
  curl -s "${SERVICE_URL}/replay/${pred_id}" \
    | jq '{prediction_id, reproducible, score_match, decision_match, explanation_match, input_hash_verified, explanation_hash_verified, score_diff}' \
    | tee "${LOG_DIR}/demo_replay.json"

  log "GET /consistency"
  curl -s "${SERVICE_URL}/consistency" | tee "${LOG_DIR}/demo_consistency.json"

  log "Demo stage complete. Artifacts in ${LOG_DIR}/"
}

stage_benchmark() {
  log "Stage: benchmark — p50/p95 latency vs 5s SLO"
  python3 person2_query_interface/benchmarks/run_benchmarks.py \
    | tee "${LOG_DIR}/benchmark.json"
  python3 - <<'PY'
import json
with open("person2_query_interface/benchmarks/results/latest.json") as f:
    r = json.load(f)
for s in r["scales"]:
    print(f"{s['record_count']:>6} rec | p50={s['latency_ms']['p50']:>7} ms | p95={s['latency_ms']['p95']:>7} ms | within_slo={s['within_slo']}")
PY
  log "Benchmark complete."
}

stage_compliance() {
  log "Stage: compliance — Person 3 compliance report"
  (cd person3_compliance_audit && python3 -m unittest discover -s tests -v 2>&1 | tee "${LOG_DIR}/compliance_tests.log") || true
  # Generate a concise compliance summary using Person 2's engine + repository.
  python3 - <<'PY'
from person2_query_interface.app.repository import get_repository
from person2_query_interface.app.reproducibility import ReproducibilityEngine
repo = get_repository()
engine = ReproducibilityEngine(repo)
ids = repo.list_prediction_ids()
n = len(ids)
replayed = 0
consistent = engine.consistency_report()
for pid in ids[:200]:
    try:
        r = engine.replay_prediction(pid)
        if r.replayed:
            replayed += 1
    except Exception:
        pass
print("=== Compliance summary (sample) ===")
print(f"Predictions available: {n}")
print(f"Replayed (first 200): {replayed}")
print(f"Consistency: {'PASS' if consistent['consistent'] else 'FAIL'} (violations={consistent['violation_count']})")
PY
  log "Compliance stage complete."
}

stage_stop() {
  log "Stage: stop — shutdown the background service"
  if [[ -f "${PID_FILE}" ]]; then
    pid=$(cat "${PID_FILE}")
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" && log "Stopped service PID ${pid}."
    fi
    rm -f "${PID_FILE}"
  fi
}

usage() {
  cat <<EOF
Usage: $0 {install|generate|init-iceberg|test|serve|demo|benchmark|compliance|stop|all|help}

Stages:
  install        Install Python dependencies (requirements.txt)
  generate       Generate deterministic monthly Parquet batches
  init-iceberg   Initialize Iceberg warehouse and load batches
  test           Run Person 2 pytest + Person 3 unittest suites
  serve          Start FastAPI service (backend=\$EXPLANATION_REPO_BACKEND, default duckdb)
  demo           Exercise /explain, /decision, /replay, /consistency endpoints
  benchmark      Run p50/p95 latency benchmarks vs 5s SLO
  compliance     Run Person 3 compliance tests + summary
  stop           Stop the background service
  all            install -> generate -> test -> serve -> demo -> benchmark
  help           Show this help
EOF
}

cmd="${1:-help}"
case "${cmd}" in
  install)        stage_install ;;
  generate)       stage_generate ;;
  init-iceberg)   stage_init_iceberg ;;
  test)           stage_test ;;
  serve)          stage_serve ;;
  demo)           stage_demo ;;
  benchmark)      stage_benchmark ;;
  compliance)     stage_compliance ;;
  stop)           stage_stop ;;
  all)            stage_install && stage_generate && stage_test && stage_serve && stage_demo && stage_benchmark ;;
  help|*)         usage ;;
esac
