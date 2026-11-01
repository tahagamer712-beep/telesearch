"""SQL dump parser. NEVER executes SQL: it only tokenizes CREATE TABLE / INSERT
statements and reads the values. Everything else (DROP, ATTACH, PRAGMA, ...) is ignored."""
from __future__ import annotations

import re
from typing import Iterator

from ..errors import ParserError
from .base import Parser, Record
from .registry import register

_OUT = re.compile(r"[;'\"`]|--|/\*")
_IN = {"'": re.compile(r"'"), '"': re.compile(r'"'), "`": re.compile(r"`")}
_IN_BS = {"'": re.compile(r"['\\]"), '"': re.compile(r'["\\]'), "`": re.compile(r"`")}
MAX_STATEMENT = 128 * 1024 * 1024

_NAME = r"(?P<name>(?:[`\"\[]?[\w\-$]+[`\"\]]?\.)?[`\"\[]?[\w\-$ ]+?[`\"\]]?)"
_CREATE = re.compile(r"^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _NAME + r"\s*\(", re.I | re.S)
_INSERT = re.compile(r"^\s*(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+|IGNORE\s+|LOW_PRIORITY\s+|DELAYED\s+|HIGH_PRIORITY\s+)*INTO\s+" + _NAME + r"\s*(?:\((?P<cols>[^)]*)\))?\s*VALUES\s*", re.I | re.S)
_COPY = re.compile(r"^\s*COPY\s+.+\bFROM\s+stdin", re.I | re.S)
_RESERVED = {"PRIMARY", "KEY", "UNIQUE", "CONSTRAINT", "FOREIGN", "INDEX", "FULLTEXT", "CHECK", "SPATIAL", "PERIOD", "LIKE"}


def _is_column_def(tok: str) -> bool:
    """عمود حقيقي؟ الاسم المقتبس (`key`, "period") دائماً عمود؛ الكلمات المحجوزة بدون اقتباس فقط تعني قيد/فهرس."""
    if tok[:1] in "`\"[":
        return True
    return tok.upper() not in _RESERVED
_STR_BS = re.compile(r"'(?:[^'\\]|\\.|'')*'", re.S)
_STR_STD = re.compile(r"'(?:[^']|'')*'", re.S)
_DQ = re.compile(r'"(?:[^"]|"")*"', re.S)
_RAW = re.compile(r"[^,()'\"]+")
_DQ_BS = re.compile(r'"(?:[^"\\]|\\.|"")*"', re.S)
_ESC = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "Z": "\x1a", "b": "\b"}


def _unq(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in "`\"[" and s[-1] in "`\"]":
        s = s[1:-1]
    return s


def iter_statements(read, backslash: bool) -> Iterator[str]:
    """Split SQL text into statements, honoring quotes and comments."""
    carry, state, buf, esc, size = "", None, [], False, 0
    while True:
        data = read()
        last = not data
        chunk, carry = carry + data, ""
        if not chunk:
            break
        if not last and ((state is None and chunk[-1] in "-/") or (state == "/*" and chunk[-1] == "*")):
            carry, chunk = chunk[-1], chunk[:-1]
        i, n, seg = 0, len(chunk), 0
        while i < n:
            if state is None:
                m = _OUT.search(chunk, i)
                if not m:
                    i = n
                    break
                tok = m.group()
                if tok == ";":
                    buf.append(chunk[seg:m.start()])
                    yield "".join(buf)
                    buf, size = [], 0
                    seg = i = m.end()
                elif tok in _IN:
                    state, i = tok, m.end()
                else:  # comment
                    buf.append(chunk[seg:m.start()])
                    state, i = tok, m.end()
            elif state in _IN:
                if esc:
                    esc, i = False, i + 1
                    continue
                m = (_IN_BS if backslash else _IN)[state].search(chunk, i)
                if not m:
                    i = n
                    break
                if m.group() == "\\":
                    if m.end() >= n:
                        esc, i = True, n
                    else:
                        i = m.end() + 1
                else:
                    state, i = None, m.end()
            elif state == "--":
                j = chunk.find("\n", i)
                if j < 0:
                    i = n
                    break
                state, seg, i = None, j, j + 1
            else:  # /* */
                j = chunk.find("*/", i)
                if j < 0:
                    i = n
                    break
                state, seg, i = None, j + 2, j + 2
        if state is None or state in _IN:
            buf.append(chunk[seg:])
            size += n - seg
            if size > MAX_STATEMENT:
                raise ParserError("جملة SQL اكبر من الحد المسموح")
        if last:
            break
    tail = "".join(buf).strip()
    if tail:
        yield tail


def split_top(text: str) -> list[str]:
    out, depth, cur, q = [], 0, [], None
    for ch in text:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "'\"`":
            q = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _unescape(body: str, backslash: bool) -> str:
    body = body.replace("''", "'")
    if backslash and "\\" in body:
        body = re.sub(r"\\(.)", lambda m: _ESC.get(m.group(1), m.group(1)), body, flags=re.S)
    return body


def parse_tuples(s: str, pos: int, backslash: bool) -> Iterator[list[str | None]]:
    strre = _STR_BS if backslash else _STR_STD
    n = len(s)
    while pos < n:
        while pos < n and (s[pos].isspace() or s[pos] == ","):
            pos += 1
        if pos >= n or s[pos] != "(":
            return
        pos += 1
        row: list[str | None] = []
        while True:
            while pos < n and s[pos].isspace():
                pos += 1
            if pos >= n:
                raise ParserError("قيم INSERT مقطوعة")
            ch = s[pos]
            if ch in "Nn" and pos + 1 < n and s[pos + 1] == "'":  # SQL Server: N'نص'
                pos += 1
                ch = "'"
            if ch == "'":
                m = strre.match(s, pos)
                if not m:
                    raise ParserError("نص غير مغلق داخل INSERT")
                row.append(_unescape(m.group()[1:-1], backslash))
                pos = m.end()
            elif ch == '"':  # MySQL ANSI-less dumps: "نص, فيه فاصلة"
                m = (_DQ_BS if backslash else _DQ).match(s, pos)
                if not m:
                    raise ParserError("نص غير مغلق داخل INSERT")
                body = m.group()[1:-1].replace('""', '"')
                if backslash and "\\" in body:
                    body = re.sub(r"\\(.)", lambda mm: _ESC.get(mm.group(1), mm.group(1)), body, flags=re.S)
                row.append(body)
                pos = m.end()
            else:
                start, depth = pos, 0
                while pos < n:
                    c = s[pos]
                    if c == "'":
                        m = strre.match(s, pos)
                        pos = m.end() if m else n
                    elif c == "(":
                        depth += 1
                        pos += 1
                    elif c == ")":
                        if depth == 0:
                            break
                        depth -= 1
                        pos += 1
                    elif c == "," and depth == 0:
                        break
                    else:
                        pos += 1
                tok = s[start:pos].strip()
                row.append(None if tok.upper() == "NULL" else tok)
            while pos < n and s[pos].isspace():
                pos += 1
            if pos < n and s[pos] == ",":
                pos += 1
                continue
            if pos < n and s[pos] == ")":
                pos += 1
                break
            raise ParserError("صياغة قيم INSERT غير متوقعة")
        yield row


@register
class SQLParser(Parser):
    name = "sql"
    kinds = ("sql",)
    multi_dataset = True

    def records(self, skip: int = 0) -> Iterator[Record]:
        f = open(self.path, "r", encoding=self.text_encoding, errors="replace")
        try:
            head = f.read(65536)
            backslash = bool(re.search(r"mysql|mariadb|phpmyadmin|navicat|sqlyog|heidisql|ENGINE\s*=|\\'", head, re.I))
            f.seek(0)
            tables: dict[str, list[str]] = {}
            n = stmt_no = ignored = 0
            for stmt in iter_statements(lambda: f.read(1 << 20), backslash):
                stmt_no += 1
                if _COPY.match(stmt):
                    raise ParserError("PostgreSQL COPY غير مدعوم؛ صدّر القاعدة بصيغة INSERT")
                m = _CREATE.match(stmt)
                if m:
                    body = stmt[m.end():]
                    end = body.rfind(")")
                    cols = []
                    for d in split_top(body[:end] if end >= 0 else body):
                        first = d.strip().split(None, 1)
                        if first and _is_column_def(first[0]):
                            cols.append(_unq(first[0]))
                    tables[_unq(m["name"]).split(".")[-1]] = cols
                    continue
                m = _INSERT.match(stmt)
                if not m:
                    ignored += 1  # DROP/ATTACH/PRAGMA/SET/... لا تُنفَّذ ابداً
                    continue
                table = _unq(m["name"]).split(".")[-1]
                cols = [_unq(c) for c in m["cols"].split(",")] if m["cols"] else tables.get(table, [])
                for t, row in enumerate(parse_tuples(stmt, m.end(), backslash), 1):
                    n += 1
                    if n <= skip:
                        continue
                    names = cols if len(cols) >= len(row) else cols + [f"col_{i + 1}" for i in range(len(cols), len(row))]
                    yield Record(table, f"table={table};stmt={stmt_no};tuple={t}", dict(zip(names, row)))
            if ignored:
                self.warnings.append(f"تم تجاهل {ignored} جملة SQL غير INSERT/CREATE (لم تُنفَّذ)")
        finally:
            f.close()
