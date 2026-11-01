"""Formatting helpers for Telegram screens."""
from __future__ import annotations

import time

from ..auth import User
from ..search.present import ResultRecord, mask_for

STATUS_ICON = {"indexed": "🟢", "partial": "🟡", "failed": "🔴", "quarantined": "🔴", "queued": "⏳", "received": "⏳", "processing": "⏳", "unsupported": "⚪",
               "running": "▶️", "paused": "⏸", "interrupted": "⚠️", "done": "✅", "cancelled": "🚫"}
LINE = "━━━━━━━━━━━━━━━━━━"


def kb(*rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def nav(back: str | None = None):
    return [("🏠 الرئيسية", "h")] + ([("◀️ رجوع", back)] if back else [])


def pager(page: int, pages: int, prefix: str) -> list:
    """Pagination row with only real moves: no dead arrows at bounds, no row on a single page.
    Page info belongs in the screen title, not in a dead middle button."""
    row = []
    if page > 0:
        row.append(("◀", f"{prefix}:{page - 1}"))
    if page < pages - 1:
        row.append(("▶", f"{prefix}:{page + 1}"))
    return row


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return str(n)


def fmt_int(n) -> str:
    return f"{int(n or 0):,}"


def chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for para in text.split("\n"):
        while len(para) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 1 > limit:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def result_text(rr: ResultRecord, user: User) -> str:
    rr = mask_for(rr, user)
    lines = [f"🔎 RESULT #{rr.rank}"]
    for name, _role, val in rr.fields:
        lines.append(f"{name}: {val}")
    loc = rr.locator or "Not available"
    sheet = next((p.split("=", 1)[1] for p in loc.split(";") if p.startswith("sheet=")), None)
    row = next((p.split("=", 1)[1] for p in loc.split(";") if p.startswith(("row=", "rowid=", "line=", "index="))), None)
    lines += [LINE, f"📄 Source: {rr.file_name}"]
    if sheet:
        lines.append(f"📑 Sheet: {sheet}")
    lines.append(f"📍 Row: {row or loc}")
    vals = {n: v for n, _r, v in rr.fields}
    for col in rr.matched[:3]:
        lines.append(f"🧩 Column: {col}\n🔤 Original value: {vals.get(col, 'Not available')}")
    if not rr.matched:
        lines.append("🧩 Column: Not available")
    lines.append(f"🎯 Match: {rr.label} {rr.label_text}")
    return "\n".join(lines)


# Arabic DISPLAY words for the jobs-list button only. The underlying
# kind/status values in the database are untouched; unknown values fall back
# to the raw value so the button never breaks on a future kind/status.
JOB_KIND_AR = {"ingest": "فهرسة", "er_match": "مطابقة", "er_export": "تصدير المطابقة", "export": "تصدير", "master_excel": "دمج رئيسي", "notify": "إشعار"}
JOB_STATUS_AR = {"queued": "معلّقة", "running": "جارية", "paused": "موقوفة", "interrupted": "متقطعة", "done": "منتهية", "cancelled": "ملغاة", "failed": "فاشلة", "received": "مستلمة", "processing": "قيد المعالجة", "partial": "جزئية", "indexed": "مفهرسة", "unsupported": "غير مدعومة", "quarantined": "محجورة"}


def job_line(j) -> str:
    return f"{STATUS_ICON.get(j['status'], '•')} JOB-{j['id']:06d} {JOB_KIND_AR.get(j['kind'], j['kind'])} {JOB_STATUS_AR.get(j['status'], j['status'])} {j['progress']:.0f}%"


def ago(ts: str | None) -> str:
    return ts.replace("T", " ")[:19] if ts else "-"


def unified_text(ur, user: User) -> str:
    """Unified entity display (preferred values + sources). Raw record fallback for non-ER rows."""
    from ..er import entities
    if ur.view is None:
        return result_text(ur.record, user) if ur.record else "-"
    v = ur.view
    lines = [f"🔎 RESULT #{ur.rank}", f"Unified ID: {v['public_id']}"]
    for sf in entities.IDENTITY_SINGLE:
        f = v["fields"].get(sf)
        if f:
            extra = f"  ⚠️ ({len(f['known'])} قيم)" if f["conflict"] else ""
            lines.append(f"{entities.FIELD_AR[sf]}: {f['preferred']}{extra}")
    for x in v["extras"][:5]:
        lines.append(f"{x['field']}: {x['value']}")
    seen, srcs = set(), []
    for f in v["fields"].values():
        for k in f["known"]:
            for s in k["sources"]:
                if s["record_id"] not in seen:
                    seen.add(s["record_id"]); srcs.append(s)
    for ev in v["events"]:
        if ev["source"]["record_id"] not in seen:
            seen.add(ev["source"]["record_id"]); srcs.append(ev["source"])
    lines += [LINE, f"📚 SOURCES: {len(v['record_ids'])}"]
    for i, s in enumerate(srcs[:5], 1):
        lines.append(f"{i}. {(s['archive'] + ' → ') if s['archive'] else ''}{s['file']}\n   {s['locator']}")
    if len(srcs) > 5:
        lines.append(f"… +{len(srcs) - 5} (📚 للتفاصيل)")
    lines += [LINE, f"🔗 MATCH: {entities.STATE_LABEL.get(v['state'], v['state'])}", f"🎯 Search: {ur.label} {ur.label_text} ({ur.hit_count} سجل)"]
    for e in ur.evidence[:3]:
        src = f"{(e['archive'] + ' → ') if e.get('archive') else ''}{e['file']} / {e['locator']}"
        lines.append(f"   ↳ «{e['value']}»" + (" (variant)" if e["variant"] else "") + f" [{e['field']}] ← {src}")
    if v["open_issues"]:
        lines.append("⚠️ مراجعة معلّقة: " + ", ".join(f"{k}={n}" for k, n in v["open_issues"].items()))
    return "\n".join(lines)


def _src(s: dict) -> str:
    imp = f" • استُورد {s['imported_at'][:19].replace('T', ' ')}" if s.get("imported_at") else ""
    return f"{(s['archive'] + ' → ') if s.get('archive') else ''}{s['file']} / {s['locator']}{imp}"


def profile_text(v: dict) -> str:
    """Unified Record Profile: everything is read from imported data; each value carries its source."""
    from ..er import entities
    lines = ["📇 Unified Record Profile", f"Unified Entity ID: {v['public_id']}",
             f"Match status: {entities.STATE_LABEL.get(v['state'], v['state'])}",
             f"Match confidence: {('%.2f' % v['confidence']) if v['confidence'] is not None else '-'} (matcher v{v['matcher_version']}؛ الاضعف بين الروابط)", LINE]
    for sf in entities.IDENTITY_SINGLE:
        f = v["fields"].get(sf)
        if not f:
            continue
        lines.append(f"{entities.FIELD_AR[sf]}" + (" ⚠️ قيم مختلفة (لا يوجد اختيار تلقائي نهائي)" if f["conflict"] else ""))
        for k in f["known"]:
            star = " ★ preferred" if k is f["known"][0] and len(f["known"]) > 1 else ""
            variants = [x for x in k["variants"] if x != k["value"]]
            lines.append(f"  • {k['value']}{star}  [{k['n_sources']} مصدر]" + (f"\n     صيغ مكتوبة: {' | '.join(variants)}" if variants else ""))
            for s in k["sources"][:4]:
                lines.append(f"     ↳ {_src(s)}  (العمود: {s['column']})")
            if len(k["sources"]) > 4:
                lines.append(f"     … +{len(k['sources']) - 4} مصادر")
    if v["events"]:
        lines += [LINE, f"📅 الاحداث ({len(v['events'])})"]
        for ev in v["events"][:6]:
            lines.append("• " + ", ".join(f"{k}={val}" for k, val in list(ev["fields"].items())[:6]) + f"\n   ↳ {_src(ev['source'])}")
        if len(v["events"]) > 6:
            lines.append(f"… +{len(v['events']) - 6}")
    if v["extras"]:
        lines += [LINE, f"حقول اضافية محفوظة ({len(v['extras'])})"]
        for x in v["extras"][:6]:
            lines.append(f"• {x['field']}: {x['value']}\n   ↳ {_src(x['source'])}")
    lines += [LINE, f"🗂 السجلات الاصلية المرتبطة ({len(v['originals'])})"]
    for i, o in enumerate(v["originals"][:12], 1):
        lines.append(f"{i}. #{o['record_id']} {_src(o)}" + (" (صف مكرر)" if o.get("duplicate_of") else ""))
    if len(v["originals"]) > 12:
        lines.append(f"… +{len(v['originals']) - 12} (Source Values في Unified Excel)")
    if v["edges"]:
        lines += [LINE, "🔎 ادلة المطابقة"]
        for e in v["edges"][:5]:
            import json as _j
            ev = _j.loads(e["evidence"])
            lines.append(f"• #{e['record_a']}↔#{e['record_b']} {e['state']} {e['confidence']:.2f} (قاعدة {ev.get('rule')}): " + ", ".join(f"{i['field']}={i['kind']}" for i in ev.get("items", [])))
    if v["open_issues"]:
        lines.append("⚠️ مراجعة معلّقة: " + ", ".join(f"{k}={n}" for k, n in v["open_issues"].items()))
    return "\n".join(lines)
