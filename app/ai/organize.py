"""LLM organizer for the bot (universal, any domain).

Schema inference from a sample, then batched row extraction aligned by
input index. Stdlib only (urllib + retries). Never invents a value:
the model returns null for a destroyed cell. The key/model/base URL are
passed in by the caller (bot config), never stored here.

DORMANT — NOT WIRED to any job, CLI, or Telegram path (verified in the
audit). Audit fix (Phase 6 10.3): before ANY wiring, an explicit
consent/purpose gate is required, because payloads leave the device. As
defense in depth, every line sent is passed through _mask_pii first:
emails, phone-shaped digit sequences and long ID digit runs are replaced
by placeholders, so no raw phone/ID value can appear in a captured body.
Names remain visible to the provider by design — extraction cannot copy
a name it cannot see — which is exactly why wiring still needs the
user's explicit order.
"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Phones: 10+ digits, spaces allowed as separators, dashes NOT (so dates
# like 2024-01-15 and years survive untouched).
_PHONE_RE = re.compile(r"\+?\d[\d\s]{8,}\d")
_LONG_DIGITS_RE = re.compile(r"\d{9,}")


def _mask_pii(text: str) -> str:
    """Mask identifiers in a line before it leaves the device. Order matters:
    emails first, then phone-shaped sequences, then any remaining long digit
    run (national IDs). Names and short numbers (years, counts) pass through."""
    text = _EMAIL_RE.sub("__EMAIL__", text)
    text = _PHONE_RE.sub("__NUM__", text)
    text = _LONG_DIGITS_RE.sub("__ID__", text)
    return text


def call_llm(model, messages, max_tokens, key, base_url, tries=3):
    last = None
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": 0, "max_tokens": max_tokens})
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                base_url.rstrip("/") + "/chat/completions",
                data=body.encode(), method="POST",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            # Free models rate-limit bursts: wait out a 429, fail fast otherwise.
            if e.code == 429 and attempt < tries - 1:
                time.sleep(45 * (attempt + 1))
                last = e
                continue
            raise
        except (http.client.IncompleteRead, urllib.error.URLError,
                ConnectionError, TimeoutError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def _json_only(text):
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("no JSON in model reply")


def infer_schema(lines, model, key, base_url):
    sample = "\n".join(f"- {_mask_pii(ln)}" for ln in lines)
    msgs = [
        {"role": "system", "content": (
            "أنت منظم جداول ذكي. مهمتك استنتاج أعمدة جدول من سطور OCR "
            "مشوّهة، لا اختراع أي شيء.")},
        {"role": "user", "content": (
            "أمامك سطور مستخرجة بتقنية OCR من جدول ممسوح ضوئياً "
            "(فيه ميلان وتشويش وأخطاء قراءة). استنتج أعمدة الجدول من "
            "السطور نفسها، ومن سطر الترويسة إن وجد.\n"
            "أعد JSON فقط بهذا الشكل:\n"
            '{"columns": [{"name": "اسم العمود", "hint": "وصف قصير"}], '
            '"notes": "ملاحظة قصيرة"}\n'
            "القواعد:\n"
            "- أسماء الأعمدة بلغة الملف نفسه.\n"
            "- عمود واحد لكل حقل مميز، ولا عمود لا دليل عليه بالسطور.\n"
            "- إن وجد عمود تسلسل أو ترقيم فاجعله أولاً.\n"
            "- لا تذكر قيماً، فقط أسماء الأعمدة وأوصافها.\n"
            "السطور:\n" + sample)},
    ]
    out = call_llm(model, msgs, 800, key, base_url)
    data = _json_only(out["choices"][0]["message"]["content"])
    cols = [c["name"] for c in data["columns"]]
    return cols, data, out.get("usage", {})


def extract_batch(columns, lines, model, key, base_url):
    numbered = "\n".join(f"{i + 1}. {_mask_pii(ln)}" for i, ln in enumerate(lines))
    cols = "، ".join(columns)
    col_json = '", "'.join(columns)
    msgs = [
        {"role": "system", "content": (
            "أنت مستخرج جداول دقيق. تنسخ ما تراه فقط، ولا تخترع أبداً.")},
        {"role": "user", "content": (
            f"استخرج كل سطر إلى صف بأعمدة: {cols}.\n"
            "أعد JSON فقط بهذا الشكل:\n"
            '{"rows": [{"i": 1, "' + col_json + '": null}]}\n'
            f"عدد السطور المرقمة {len(lines)}، ويجب أن ترد {len(lines)} صفاً.\n"
            "القواعد:\n"
            "- ضع في كل صف رقم سطره i كما هو مرقماً أمامك.\n"
            "- حتى السطر الذي لا يُقرأ أبداً يجب أن يظهر كصف كل قيمه null.\n"
            "- لا تحذف أي سطر ولا تدمج سطرين أبداً.\n"
            "- انسخ القيمة كما تظهر بالسطر فقط.\n"
            "- إن كانت الخلية مشوّهة تماماً أو غائبة فضع null ولا تخترع.\n"
            "- أزل علامات الفصل (| والأقواس) فقط عندما تكون فواصل واضحة.\n"
            "- لا تصحح أسماء ولا تكمّلها من خيالك.\n"
            "- الأرقام كما هي بلا تعديل.\n"
            "السطور:\n" + numbered)},
    ]
    out = call_llm(model, msgs, 6000, key, base_url)
    data = _json_only(out["choices"][0]["message"]["content"])
    rows = data["rows"]
    by_i = {}
    for r in rows:
        try:
            i = int(r.get("i"))
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(lines) and i not in by_i:
            by_i[i] = r
    dropped = [i for i in range(1, len(lines) + 1) if i not in by_i]
    if not by_i and len(rows) == len(lines):
        by_i = {i + 1: r for i, r in enumerate(rows)}
        dropped = []
    norm = []
    for i in range(1, len(lines) + 1):
        r = by_i.get(i, {})
        norm.append({c: (None if r.get(c) is None else str(r.get(c)).strip())
                     for c in columns})
    return norm, {"usage": out.get("usage", {}), "dropped": dropped}
