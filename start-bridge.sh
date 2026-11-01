#!/bin/bash
# MTProto bridge launcher — PRODUCTION private_database (Nokia).
# Prepared 2026-10-29; NOT YET LAUNCHED (no live download test possible).
# Uses a COPY of the job_hunter Telethon session; the original is untouched.
# Secrets come from the EXISTING job_hunter .env at runtime; nothing
# secret is stored here or printed. Safe to run repeatedly: a second
# instance is refused by run/bridge.lock inside the service.
cd "$(dirname "$0")"

if [ -z "$TELEGRAM_API_ID" ] && [ -f ~/job_hunter/.env ]; then
  set -a
  # shellcheck disable=SC1090
  . ~/job_hunter/.env
  set +a
fi

export MTPROTO_SESSION_PATH="${MTPROTO_SESSION_PATH:-$HOME/private_database/sessions/mtproto_bridge.session}"
export MTPROTO_BOT_PEER="${MTPROTO_BOT_PEER:-@iraqdatabassbot}"
export PDB_BASE_DIR="${PDB_BASE_DIR:-$HOME/private_database}"
export MTPROTO_PREMIUM="${MTPROTO_PREMIUM:-0}"
export MTPROTO_POLL_SEC="${MTPROTO_POLL_SEC:-30}"

PY=python
command -v python >/dev/null || PY=python3
if [ -x ~/mtproto-venv/bin/python ]; then
  PY=~/mtproto-venv/bin/python
elif [ -x ~/bmo-test-venv/bin/python ]; then
  PY=~/bmo-test-venv/bin/python
fi

mkdir -p logs run
nohup "$PY" -m app.mtproto.service >> logs/bridge.log 2>&1 &
echo "production bridge launched (see logs/bridge.log; a duplicate is refused by lock)"
