#!/data/data/com.termux/files/usr/bin/bash
cd "$(dirname "$0")"
PY=python; command -v python >/dev/null || PY=python3
$PY health_check.py "$@"
echo "---- التوافق"; $PY -m app.compat
