#!/data/data/com.termux/files/usr/bin/bash
# تثبيت Private Database على Termux. الاستخدام: ./install.sh [--optional]
# --optional: يحاول تثبيت الادوات الاختيارية (p7zip unrar poppler openssl-tool tmux). فشلها لا يوقف التثبيت.
set -u
cd "$(dirname "$0")"
PY=python; command -v python >/dev/null || PY=python3
echo "== 1) فحص Python"; $PY --version || { echo "MISSING: Python — شغّل: pkg install python"; exit 1; }
$PY - <<'PY' || exit 1
import sys
assert sys.version_info >= (3, 10), "يلزم Python 3.10+"
PY
if [ "${1:-}" = "--optional" ]; then
  echo "== 2) ادوات اختيارية"
  for p in p7zip unrar poppler openssl-tool tmux; do
    pkg install -y "$p" >/dev/null 2>&1 && echo "  ✓ $p" || echo "  ✗ $p (غير متاح/فشل — الميزة المرتبطة بها ستكون OPTIONAL/MISSING)"
  done
fi
echo "== 3) ملف الاعدادات"
if [ ! -f .env ]; then cp .env.example .env; echo "تم انشاء .env"; fi
chmod 600 .env
echo "== 4) المجلدات وقاعدة البيانات"
$PY scripts/init_db.py || exit 1
echo "== 5) تقرير التوافق (SUPPORTED / MISSING / OPTIONAL / NEEDS_TEST)"
$PY -m app.compat
echo "== 5.5) اعتماديات الاختبار (pypdf/openpyxl — الفشل لا يوقف التثبيت)"
$PY -m pip install -q -r requirements-test.txt >/dev/null 2>&1 && echo "  ✓ test deps" || echo "  ✗ test deps (بعض الاختبارات قد تفشل عند الجمع — ثبّتها يدوياً: pip install -r requirements-test.txt)"
echo "== 6) اختبارات ذاتية"
$PY -m unittest discover -s tests 2>&1 | tail -4
chmod +x start.sh stop.sh status.sh backup.sh
echo; echo "جاهز. الخطوات التالية:  nano .env   (TELEGRAM_BOT_TOKEN, AUTHORIZED_USER_IDS, BACKUP_PASSPHRASE)  ثم  ./start.sh"
echo "تذكير: ما يظهر NEEDS_TEST اعلاه لم يُختبر على جهازك بعد؛ راجع COMPATIBILITY.md"
