#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# KubeAgent demo launcher
#
# Creates (or reuses) a local virtual environment, installs dependencies and
# starts the server on http://localhost:8000, opening the dashboard in the
# default browser once /healthz answers.
#
# Usage:
#   ./run_demo.sh              # port 8000, simulation mode, heuristic engine
#   PORT=8080 ./run_demo.sh
#   DEEPSEEK_API_KEY=sk-... ./run_demo.sh    # enable LLM reasoning
#
# No cluster or kubeconfig is required: SIMULATION_MODE defaults to true, so
# the collectors serve the deterministic chaos scenarios.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
URL="http://localhost:${PORT}"

log() { printf '\033[1;32m[KubeAgent]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[KubeAgent]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[KubeAgent]\033[0m %s\n' "$*" >&2; exit 1; }

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "$PYTHON_BIN not found. Install Python 3.11+ and retry."

# --- 1. Virtual environment ------------------------------------------------
if [ ! -x "$VENV_DIR/bin/python" ]; then
  log "Creating virtual environment in $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR" || fail "could not create a virtual environment (install python3-venv)"
fi

# --- 2. Dependencies -------------------------------------------------------
log "Installing dependencies (safe to re-run)"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -r "$ROOT_DIR/requirements.txt" \
  || fail "dependency installation failed"

# --- 3. Sanity check: the app imports -------------------------------------
log "Checking the application imports"
( cd "$ROOT_DIR/backend" && "$VENV_DIR/bin/python" -c "import app.main" ) \
  || fail "KubeAgent failed to import; see the traceback above"

# --- 4. Reasoning engine notice -------------------------------------------
if [ -n "${DEEPSEEK_API_KEY:-}" ] || [ -n "${LLM_API_KEY:-}" ]; then
  log "LLM key detected — DeepSeek reasoning enabled"
else
  warn "No DEEPSEEK_API_KEY / LLM_API_KEY set — using the built-in heuristic engine (this is fine for the demo)"
fi

# --- 5. Launch -------------------------------------------------------------
if command -v curl >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  fail "port ${PORT} is already serving; stop that process or run PORT=8080 ./run_demo.sh"
fi

log "Starting KubeAgent on ${URL}"
# uvicorn runs as a direct child so the cleanup trap can always stop it.
"$VENV_DIR/bin/python" -m uvicorn app.main:app \
  --app-dir "$ROOT_DIR/backend" \
  --host "$HOST" --port "$PORT" &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$SERVER_PID" 2>/dev/null || return 0
    sleep 0.25
  done
  kill -9 "$SERVER_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# --- 6. Wait for readiness, then open the browser -------------------------
ready=0
for _ in $(seq 1 60); do
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS "http://${HOST}:${PORT}/healthz" >/dev/null 2>&1; then ready=1; break; fi
  elif "$VENV_DIR/bin/python" - "$HOST" "$PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(f"http://{sys.argv[1]}:{sys.argv[2]}/healthz", timeout=1).read()
PY
  then ready=1; break; fi
  sleep 0.5
done

if [ "$ready" -eq 1 ]; then
  log "Ready — dashboard: ${URL}/  · docs: ${URL}/docs"
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "${URL}/" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then open "${URL}/" >/dev/null 2>&1 || true
  elif command -v start >/dev/null 2>&1; then start "${URL}/" >/dev/null 2>&1 || true
  else warn "Open ${URL}/ in your browser"
  fi
  log "Trigger the demo failure with:"
  printf '  curl -s -X POST %s/api/v1/incidents/simulate -H "Content-Type: application/json" -d '"'"'{"scenario":"OOMKilled"}'"'"'\n' "$URL"
else
  warn "Server did not answer /healthz yet — check the log above; try ${URL}/healthz manually"
fi

wait "$SERVER_PID"
