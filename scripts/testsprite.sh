#!/usr/bin/env bash
# ============================================================================
# Run a SECOND Closebook, for automated testers (TestSprite), beside the real
# one.
#
# WHY THIS EXISTS
#   An automated tester drives every screen it can reach, presses buttons it
#   does not understand, and submits forms with invented data. Pointed at the
#   operator's workspace that is not a test, it is an incident: it would settle
#   real bills, spend the committed-cash budget, and fill the audit log with
#   actions nobody took. So this instance gets:
#
#     * its OWN SQLite file (data/testsprite.sqlite3) — never the demo book
#     * its OWN operator name, so every row it writes says "testsprite"
#     * its OWN PIN, so the operator's PIN is not on a page bots can reach
#     * its OWN port (8020) and its own public URL
#
#   It shares the Airwallex SANDBOX keys and the NVIDIA LLM config, because the
#   point is to exercise the same code against the same sandbox APIs. No real
#   money exists anywhere in this project.
#
# SAFETY
#   The database path is checked before anything starts. The demo file
#   (data/closebook.sqlite3) is refused outright, by name, at the top of the
#   script rather than trusted to stay correct further down.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"

PORT="${TESTSPRITE_PORT:-8020}"
DB="${TESTSPRITE_DB:-${ROOT}/data/testsprite.sqlite3}"
DB_BASENAME="$(basename "${DB}")"

# ---- guards ----------------------------------------------------------------
if [ "${DB_BASENAME}" != "testsprite.sqlite3" ] \
   && [ "${TESTSPRITE_ALLOW_OTHER_DB:-0}" != "1" ]; then
  echo "refused: the database must be data/testsprite.sqlite3 (got ${DB_BASENAME})." >&2
  echo "         set TESTSPRITE_ALLOW_OTHER_DB=1 only if you truly mean another file." >&2
  exit 1
fi
case "${DB}" in
  */closebook.sqlite3|*/closebook.sqlite3.*)
    echo "refused: that is the DEMO book. This instance must never open it." >&2
    exit 1 ;;
esac
if [ ! -x "${PY}" ]; then
  echo "refused: no interpreter at ${PY}. Run 'make setup' first." >&2
  exit 1
fi

# ---- the PIN ---------------------------------------------------------------
# Read TESTSPRITE_PIN out of .env without sourcing the file: .env carries JSON
# values (LLM_EXTRA_BODY) that a shell cannot safely evaluate.
read_env() {
  "${PY}" - "$1" <<'PYEOF'
import re, sys
name = sys.argv[1]
try:
    text = open(".env").read()
except OSError:
    print(""); raise SystemExit
m = re.search(rf"^{re.escape(name)}=(.*)$", text, re.M)
print((m.group(1).strip() if m else ""))
PYEOF
}

PIN="${TESTSPRITE_PIN:-}"
if [ -z "${PIN}" ] && [ -f "${ROOT}/.env" ]; then
  PIN="$(cd "${ROOT}" && read_env TESTSPRITE_PIN)"
fi
if [ -z "${PIN}" ]; then
  echo "refused: TESTSPRITE_PIN is not set. Add it to .env — this instance must" >&2
  echo "         not fall back to the operator's PIN, and an unset PIN closes" >&2
  echo "         every approval route rather than opening them." >&2
  exit 1
fi

BASE_URL="${TESTSPRITE_BASE_URL:-}"
if [ -z "${BASE_URL}" ] && [ -f "${ROOT}/.env" ]; then
  BASE_URL="$(cd "${ROOT}" && read_env TESTSPRITE_BASE_URL)"
fi

# ---- run -------------------------------------------------------------------
mkdir -p "${ROOT}/data" "${ROOT}/logs"

export CLOSEBOOK_DATABASE_URL="sqlite:///${DB}"
export OPERATOR_NAME="${OPERATOR_NAME:-testsprite}"
# WHO WE ARE, and what we refuse. Both instances share ONE Airwallex sandbox
# account, so each sees the other's deposits in `GET /deposits` and receives
# the other's transfer events. CLOSEBOOK_INSTANCE is stamped onto everything
# this instance creates, so the operator's instance can recognise it and ignore
# it; CLOSEBOOK_INGEST_UNCLAIMED=0 additionally stops this instance sweeping up
# the OPERATOR's deposits, or any genuine arrival, as if it were a test.
export CLOSEBOOK_INSTANCE="${CLOSEBOOK_INSTANCE:-testsprite}"

# PER-TEST BOOKS. Each `?ws=<suffix>` gets its own SQLite file, so 63 tests do
# not share one set of commitments, bill numbers and approvals. Gated on this
# variable, which only this script sets: on the operator's instance a `?ws=`
# request is refused rather than served from someone else's book.
export CLOSEBOOK_WORKSPACES=1
export CLOSEBOOK_WORKSPACE_ROOT="${ROOT}/data/workspaces"
export CLOSEBOOK_INGEST_UNCLAIMED=0
# ONE PIN CONCEPT. The gate reads DEMO_APPROVER_PIN and knows nothing about
# which instance it is in; the VALUE is what differs. Keeping the name in .env
# as TESTSPRITE_PIN and pointing the gate at it here means the code never grows
# a second kind of PIN.
export DEMO_APPROVER_PIN="${PIN}"
if [ -n "${BASE_URL}" ]; then
  export PUBLIC_BASE_URL="${BASE_URL}"
fi

echo "TestSprite instance"
echo "  database : ${DB}"
echo "  operator : ${OPERATOR_NAME}"
echo "  instance : ${CLOSEBOOK_INSTANCE}  (deposits it creates are tagged, and it"
echo "            ignores everything that is not its own)"
echo "  port     : ${PORT}"
echo "  PIN      : set (${#PIN} characters, from TESTSPRITE_PIN) — not printed"
if [ -n "${BASE_URL}" ]; then echo "  base url : ${BASE_URL}"; fi
echo "  a11y     : this instance is SEPARATE from the operator's workspace"
echo

cd "${ROOT}/backend"
# ONE WORKER, DELIBERATELY. Two workers would give a stalled event loop less
# reach, but this app runs background work INSIDE its own process: the cycle
# scheduler, the live-status refresh and the watchdogs. Every additional worker
# runs all of them again, so the cost is not throughput, it is the same job
# performed twice against one sandbox and one ledger. The 502s that made two
# workers look attractive were a DDL race in `get_default_db()`, which is fixed
# at its root (a lock plus a race-tolerant create_all), so the reason is gone.
# If a second worker is ever wanted, the background loops must first take a row
# in the database that names the runner.
exec "${PY}" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" \
    --log-level warning
