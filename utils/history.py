"""Local processing history: run *metadata only* (no document content) in reports/history.json."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from config.settings import SETTINGS
from core.models import ProcessingResult

_LOCK = threading.Lock()
MAX_ENTRIES = 200


def _path() -> Path:
    return SETTINGS.reports_dir / "history.json"


def load_history() -> list[dict[str, Any]]:
    if not SETTINGS.history_enabled:
        return []
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record_run(result: ProcessingResult) -> None:
    """Append one run's metrics. Never stores extracted values."""
    if not SETTINGS.history_enabled:
        return
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "file_name": result.file_name,
        "file_hash": result.file_hash[:16],
        "doc_type": result.doc_type,
        **result.metrics(),
    }
    with _LOCK:
        items = load_history()
        items.append(entry)
        items = items[-MAX_ENTRIES:]
        try:
            _path().parent.mkdir(parents=True, exist_ok=True)
            tmp = _path().with_suffix(".tmp")
            tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
            tmp.replace(_path())
        except OSError:
            pass


def clear_history() -> None:
    with _LOCK:
        try:
            _path().unlink(missing_ok=True)
        except OSError:
            pass


def lifetime_totals(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "documents": len(items),
        "pages": sum(int(i.get("pages", 0)) for i in items),
        "tables": sum(int(i.get("tables", 0)) for i in items),
        "rows": sum(int(i.get("rows", 0)) for i in items),
        "issues": sum(int(i.get("critical", 0)) + int(i.get("warnings", 0)) for i in items),
        "critical": sum(int(i.get("critical", 0)) for i in items),
    }
