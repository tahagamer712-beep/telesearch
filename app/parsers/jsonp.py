"""JSON (streaming top-level array) and JSONL parsers."""
from __future__ import annotations

import io
import json
from typing import Iterator

from ..errors import ParserError
from .base import Parser, Record, flatten
from .registry import register


@register
class JSONLParser(Parser):
    name = "jsonl"
    kinds = ("jsonl",)

    def records(self, skip: int = 0) -> Iterator[Record]:
        n = 0
        with open(self.path, "r", encoding=self.text_encoding, errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                # نحلل قبل العدّ: السطر التالف لا يُنتج سجلاً فلا يدخل n (يطابق ordinal الفهرس عند الاستئناف)
                try:
                    obj = json.loads(line)
                except ValueError as exc:
                    if n >= skip:
                        self.error_count += 1
                        if len(self.warnings) < 50:
                            self.warnings.append(f"JSONL سطر {lineno}: {exc}")
                    continue
                n += 1
                if n <= skip:
                    continue
                yield Record("main", f"line={lineno}", flatten(obj))


@register
class JSONParser(Parser):
    name = "json"
    kinds = ("json",)
    CHUNK = 1 << 20

    def records(self, skip: int = 0) -> Iterator[Record]:
        size = self.path.stat().st_size
        with open(self.path, "r", encoding=self.text_encoding, errors="replace") as f:
            first = ""
            while True:
                ch = f.read(1)
                if not ch:
                    return
                if not ch.isspace():
                    first = ch
                    break
            if first == "[":
                yield from self._stream_array(f, skip)
                return
            if first != "{":
                raise ParserError("JSON لا يبدأ بـ [ او {")
            if size > self.cfg.json_max_object_mb * 1024 * 1024:
                raise ParserError(f"JSON كائن كبير ({size >> 20}MB) وغير قابل للـstreaming؛ حوّله الى مصفوفة او JSONL")
            try:
                obj = json.loads("{" + f.read())
            except ValueError as exc:
                raise ParserError(f"JSON غير صالح: {exc}") from exc
        lists = [(k, v) for k, v in obj.items() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v[:20])]
        if len(lists) == 1:
            key, items = lists[0]
            for i, item in enumerate(items, 1):
                if i > skip:
                    yield Record("main", f"{key}[{i}]", flatten(item))
        elif skip == 0:
            yield Record("main", "object", flatten(obj))

    def _stream_array(self, f: io.TextIOBase, skip: int) -> Iterator[Record]:
        dec = json.JSONDecoder()
        st = {"buf": "", "idx": 0, "eof": False}

        def fill() -> None:
            more = f.read(self.CHUNK)
            if not more:
                st["eof"] = True
                return
            st["buf"] = st["buf"][st["idx"]:] + more
            st["idx"] = 0
            cap = int(getattr(self.cfg, "json_max_object_mb", 64) or 64) * 1024 * 1024
            if len(st["buf"]) > cap:
                raise ParserError("عنصر JSON اكبر من الحد المسموح او الملف تالف (توقف لحماية الذاكرة)")

        n = 0
        while True:
            while True:
                buf, idx = st["buf"], st["idx"]
                while idx < len(buf) and (buf[idx].isspace() or buf[idx] == ","):
                    idx += 1
                st["idx"] = idx
                if idx < len(buf):
                    break
                if st["eof"]:
                    raise ParserError("JSON مقطوع: المصفوفة لم تُغلق بـ ]")
                fill()
            buf, idx = st["buf"], st["idx"]
            if buf[idx] == "]":
                return
            try:
                obj, end = dec.raw_decode(buf, idx)
            except json.JSONDecodeError as exc:
                if st["eof"]:
                    raise ParserError(f"JSON غير صالح: {exc}") from exc
                fill()
                continue
            if end == len(buf) and not st["eof"] and not isinstance(obj, (dict, list, str)):
                fill()  # رقم/literal قد يكمل بالـchunk التالي
                continue
            st["idx"] = end
            n += 1
            if n > skip:
                if isinstance(obj, dict):
                    yield Record("main", f"index={n}", flatten(obj))
                else:
                    yield Record("main", f"index={n}", {"value": json.dumps(obj, ensure_ascii=False) if isinstance(obj, (list, dict)) else str(obj)})
            if st["idx"] > self.CHUNK:
                st["buf"], st["idx"] = st["buf"][st["idx"]:], 0
