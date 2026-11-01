#!/data/data/com.termux/files/usr/bin/bash
cd "$(dirname "$0")"
[ -f run/app.pid ] || { echo "غير مشغّل"; exit 0; }
pid=$(cat run/app.pid); kill -TERM "$pid" 2>/dev/null
for i in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || { echo "✅ توقف بأمان"; command -v termux-wake-unlock >/dev/null && termux-wake-unlock; rm -f run/app.pid; exit 0; }; sleep 1; done
echo "⚠️ لم يتوقف خلال 60ث؛ جرب: kill -9 $pid"; exit 1
