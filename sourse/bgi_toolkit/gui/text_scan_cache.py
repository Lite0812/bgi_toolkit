"""Persistent scan samples; display names and user rules are deliberately separate."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from PyQt6.QtCore import QStandardPaths

CACHE_VERSION = 3  # Compiler metadata must not shift real call parameters.


def _cache_path(key):
    root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)) / "bgi_toolkit" / "text_function_scans"
    digest = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()
    return root / (digest + ".json")


def _freeze(value):
    return tuple(_freeze(item) for item in value) if isinstance(value, list) else value


def load_text_scan_cache(key):
    try:
        payload = json.loads(_cache_path(key).read_text(encoding="utf-8"))
        if payload.get("version") != CACHE_VERSION or _freeze(payload.get("key")) != key:
            return None
        result = payload["result"]
        if not isinstance(result.get("groups"), list) or not isinstance(result.get("errors"), list):
            return None
        if type(result.get("scanned")) is not int or result["scanned"] < 0:
            return None
        result["signature"] = _freeze(result["signature"])
        if not result["signature"] or result["signature"][0] != key:
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None


def save_text_scan_cache(key, result):
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"version": CACHE_VERSION, "key": key, "result": result}, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def invalidate_text_scan_cache(key):
    _cache_path(key).unlink(missing_ok=True)
