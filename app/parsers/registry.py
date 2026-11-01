"""Parser registry + plugin loader (drop a module in app/plugins/parsers/)."""
from __future__ import annotations

import importlib
import logging
import pkgutil

from .base import Parser

log = logging.getLogger("pdb.parsers")
_REGISTRY: dict[str, type[Parser]] = {}
_loaded = False


def register(cls: type[Parser]) -> type[Parser]:
    for kind in cls.kinds:
        _REGISTRY[kind] = cls
    return cls


def load_all() -> None:
    global _loaded
    if _loaded:
        return
    from . import tabular, jsonp, sqlp, sqlitep, xlsxp, xlsp, pdfp, accessp, docp  # noqa: F401  (يسجّلون انفسهم)
    _loaded = True
    try:
        from .. import plugins
        import app.plugins.parsers as pp
        for mod in pkgutil.iter_modules(pp.__path__):
            importlib.import_module(f"app.plugins.parsers.{mod.name}")
    except Exception:  # plugin خاطئ لا يوقف النظام
        log.exception("plugin load failed")


def get_parser_class(kind: str) -> type[Parser] | None:
    load_all()
    return _REGISTRY.get(kind)


def supported_kinds() -> list[str]:
    load_all()
    return sorted(_REGISTRY)
