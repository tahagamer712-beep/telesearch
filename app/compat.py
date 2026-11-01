"""Compatibility report: SUPPORTED / MISSING / OPTIONAL / NEEDS_TEST.   python -m app.compat"""
from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import sys


def report() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    add = lambda n, s, d="": out.append((n, s, d))
    v = sys.version_info
    add("Python", "SUPPORTED" if v >= (3, 10) else "MISSING", f"{platform.python_version()} (الهدف 3.14.6)" + ("" if (v.major, v.minor) == (3, 14) else " — NEEDS_TEST على 3.14"))
    if (v.major, v.minor) != (3, 14):
        add("Python 3.14 runtime", "NEEDS_TEST", "لم يُختبر المشروع على 3.14 الفعلية في بيئة التطوير")
    sv = sqlite3.sqlite_version_info
    add("SQLite", "SUPPORTED" if sv >= (3, 35) else "MISSING", f"{sqlite3.sqlite_version} (يلزم >= 3.35 لـRETURNING/window functions)")
    try:
        c = sqlite3.connect(":memory:")
        try:
            c.execute("create virtual table t using fts5(x)")
        finally:
            c.close()
        add("FTS5", "SUPPORTED", "متوفر")
    except sqlite3.Error:
        add("FTS5", "MISSING", "البحث يعمل بـLIKE ابطأ")
    on_termux = "com.termux" in os.environ.get("PREFIX", "") or "TERMUX_VERSION" in os.environ
    add("Termux/Android", "SUPPORTED" if on_termux else "NEEDS_TEST", "Termux مكتشف" if on_termux else "ليس Termux: ملاحظات الاختبار على Nokia في COMPATIBILITY.md")
    m = platform.machine().lower()
    add("Architecture", "SUPPORTED" if m in ("aarch64", "arm64") else "NEEDS_TEST", f"{m} (الهدف aarch64)")
    for tool, what, pkg in (("7z", "7Z/RAR", "p7zip"), ("unrar", "RAR (بديل)", "unrar"), ("pdftotext", "PDF نصي", "poppler"), ("openssl", "تشفير النسخ", "openssl-tool"), ("tmux", "تشغيل في الخلفية", "tmux")):
        p = shutil.which(tool) or (shutil.which("7za") if tool == "7z" else None)
        add(tool, "SUPPORTED" if p else "OPTIONAL", f"{what}: {p}" if p else f"{what}: غير مثبت — pkg install {pkg}")
    add("حزم pip", "SUPPORTED", "لا توجد؛ stdlib فقط (لا خطر توافق مع 3.14)")
    for f in ("/proc/stat", "/proc/meminfo"):
        add(f, "SUPPORTED" if os.access(f, os.R_OK) else "NEEDS_TEST", "مقروء" if os.access(f, os.R_OK) else "قد يمنعه Android؛ يوجد fallback")
    add("SQLCipher", "NEEDS_TEST", "غير مستخدم؛ لم يُتحقق من توفره على Termux (انظر SECURITY.md)")
    return out


if __name__ == "__main__":
    w = max(len(n) for n, _, _ in report())
    for n, s, d in report():
        print(f"{s:11} {n:<{w}}  {d}")
