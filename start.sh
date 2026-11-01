#!/data/data/com.termux/files/usr/bin/bash
cd "$(dirname "$0")"
PY=python; command -v python >/dev/null || PY=python3
mkdir -p run logs
[ -f .env ] || { echo "❌ .env غير موجود. شغّل ./install.sh اولاً"; exit 1; }
chmod 600 .env 2>/dev/null
if $PY - <<'PY'
import sys; from pathlib import Path
sys.path.insert(0, "."); from app.lock import InstanceLock
sys.exit(0 if InstanceLock.is_locked(Path("run/app.lock")) else 1)
PY
then echo "⚠️ Bot already running — لن تبدأ نسخة ثانية."; exit 0; fi
keep=$(grep -E '^KEEP_AWAKE=' .env 2>/dev/null | cut -d= -f2)
[ "$keep" = "true" ] && command -v termux-wake-lock >/dev/null && termux-wake-lock && echo "wake-lock مفعّل"
nohup $PY -m app.main >> logs/stdout.log 2>&1 &
sleep 3
if $PY - <<'PY'
import sys; from pathlib import Path
sys.path.insert(0, "."); from app.lock import InstanceLock
sys.exit(0 if InstanceLock.is_locked(Path("run/app.lock")) else 1)
PY
then echo "✅ بدأ (pid $(cat run/app.pid 2>/dev/null))"; else echo "❌ فشل البدء:"; tail -8 logs/stdout.log; exit 1; fi
