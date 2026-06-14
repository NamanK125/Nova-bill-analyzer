#!/usr/bin/env bash
#
# Boot the prototype, point you at the UI, wait for you to finish,
# then run the test suite and show the output.
#
# Usage:   ./run.sh
#
# Requires .env to point LLM_BASE_URL at a reachable vLLM endpoint.

set -euo pipefail

cd "$(dirname "$0")"

# ─── colours ──────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  BOLD=$'\e[1m'; DIM=$'\e[2m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; CYAN=$'\e[36m'; RED=$'\e[31m'; OFF=$'\e[0m'
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; CYAN=""; RED=""; OFF=""
fi

say()  { echo -e "${BOLD}${CYAN}▶${OFF} $*"; }
ok()   { echo -e "${GREEN}✓${OFF} $*"; }
warn() { echo -e "${YELLOW}!${OFF} $*"; }
err()  { echo -e "${RED}✗${OFF} $*" >&2; }

# ─── 1 · sanity ───────────────────────────────────────────────────────

say "checking environment"
if [[ ! -f .env ]]; then
  warn ".env missing — copying from .env.example"
  cp .env.example .env
  warn "edit .env now and set LLM_BASE_URL to your vLLM endpoint, then re-run ./run.sh"
  exit 1
fi

# Pull LLM_BASE_URL out of .env so we can warn (best-effort)
LLM_URL=$(grep -E '^LLM_BASE_URL=' .env | head -1 | cut -d= -f2- || true)
if [[ -z "${LLM_URL:-}" || "$LLM_URL" == "http://localhost:8000/v1" ]]; then
  warn "LLM_BASE_URL is the default ($LLM_URL) — make sure vLLM is reachable there"
fi

# Python deps installed?
if ! python3 -c "import nova" 2>/dev/null; then
  say "installing python deps (first run)"
  python3 -m pip install -q -e ".[dev]"
  ok "deps installed"
fi

# Init SQLite
python3 -c "from nova.store.models import init_sync; init_sync()"

# Sample PDFs present?
if [[ ! -f samples/acme_bol_mismatch.pdf ]]; then
  say "generating sample BoLs"
  mkdir -p samples
  python3 -m nova.pdf.synth_bol --out samples/acme_bol_clean.pdf     --variant clean
  python3 -m nova.pdf.synth_bol --out samples/acme_bol_mismatch.pdf  --variant mismatch
  python3 -m nova.pdf.synth_bol --out samples/acme_bol_uncertain.pdf --variant uncertain
  ok "samples ready"
fi

# ─── 2 · pick a free port ─────────────────────────────────────────────

PORT="${PORT:-8080}"
if command -v lsof >/dev/null && lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  warn "port $PORT busy — trying $((PORT+1))"
  PORT=$((PORT+1))
fi

# ─── 3 · boot FastAPI ─────────────────────────────────────────────────

LOG=./data/server.log
mkdir -p ./data
say "starting FastAPI on :${PORT}  (log → $LOG)"

# Start uvicorn in the background. Disable reload so kill is clean.
python3 -m uvicorn nova.api.main:app --host 127.0.0.1 --port "$PORT" --log-level warning \
  > "$LOG" 2>&1 &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Wait until the server answers on /health
for i in $(seq 1 30); do
  if curl -fs "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    ok "server is up"
    break
  fi
  sleep 0.5
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    err "server died on startup — see $LOG"
    tail -n 40 "$LOG" >&2 || true
    exit 1
  fi
done

if ! curl -fs "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  err "server never became healthy — see $LOG"
  tail -n 40 "$LOG" >&2 || true
  exit 1
fi

URL="http://127.0.0.1:${PORT}/"

# ─── 4 · prompt the user ──────────────────────────────────────────────

cat <<EOF

  ${BOLD}Open the UI:${OFF}    ${GREEN}${URL}${OFF}

  Try the three sample buttons (clean / mismatch / uncertain).
  Try the NL query box at the bottom (e.g. "how many shipments were flagged this week?").

  ${DIM}Server log: $LOG${OFF}

EOF

# Try to open the browser automatically on macOS / Linux. Non-fatal if missing.
if command -v open    >/dev/null 2>&1; then open    "$URL" >/dev/null 2>&1 || true; fi
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true; fi

# Block until user types "done"
while true; do
  read -r -p "  type ${BOLD}done${OFF} when you're finished exploring the UI > " REPLY || REPLY="done"
  if [[ "${REPLY,,}" == "done" || "${REPLY,,}" == "d" || "${REPLY,,}" == "q" ]]; then
    break
  fi
  warn "type 'done' (or 'd') to continue"
done

# ─── 5 · shut server, run tests ───────────────────────────────────────

say "stopping server"
cleanup

echo
say "running test suite"
echo "${DIM}─────────────────────────────────────────────────────${OFF}"
python3 -m pytest tests/ -v --color=yes || TEST_EXIT=$?
echo "${DIM}─────────────────────────────────────────────────────${OFF}"

if [[ -z "${TEST_EXIT:-}" ]]; then
  ok "all tests passed"
else
  err "tests failed (exit $TEST_EXIT)"
  exit "$TEST_EXIT"
fi

echo
ok "done."
