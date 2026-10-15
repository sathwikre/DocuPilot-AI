"""Local processing history.

Every conversion is listed in reports/history.json. When file storage is on (DOCUPILOT_HISTORY_FILES, default on) each
run also gets its own folder, reports/history/<run_id>/, holding everything it produced:

  source/<file>             the uploaded document, byte for byte
  <stem>_docupilot.xlsx     the Excel workbook (replaced when clean-up options are applied again)
  <stem>_validation.json    every check result
  result.pkl                the full processing result, so the run can be reopened without reading the document again

Everything stays on this machine. Delete one run, or clear the whole history, from the History page.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import re
import shutil
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from config.settings import SETTINGS
from core.models import ProcessingResult
from utils.logging_config import get_logger

log = get_logger("history")
_LOCK = threading.Lock()
MAX_ENTRIES = 200
_RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


def _path() -> Path:
    return SETTINGS.reports_dir / "history.json"


def _runs_dir() -> Path:
    return SETTINGS.reports_dir / "history"


def run_dir(run_id: str) -> Path:
    if not _RUN_ID.match(run_id or ""):
        raise ValueError(f"not a history run id: {run_id!r}")
    return _runs_dir() / run_id


def load_history() -> list[dict[str, Any]]:
    if not SETTINGS.history_enabled:
        return []
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(items: list[dict[str, Any]]) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
    tmp.replace(_path())


def validation_json(result: ProcessingResult) -> str:
    """The validation report as JSON (the same content the Export Center offers)."""
    report = [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in i.__dict__.items()} for i in result.issues]
    return json.dumps(report, indent=2, default=str)


def _entry(result: ProcessingResult) -> dict[str, Any]:
    return {
        "file_name": result.file_name,
        "file_hash": result.file_hash[:16],
        "doc_type": result.doc_type,
        "ocr_mode": result.ocr_mode,
        "options": sorted(result.options),
        "removed_rows": len(result.removed_rows),
        **result.metrics(),
    }


def _write_outputs(folder: Path, result: ProcessingResult) -> dict[str, str]:
    """Write the workbook, validation report and saved result. Returns {kind: path relative to the run folder}."""
    stem = Path(result.file_name).stem
    files = {"workbook": f"{stem}_docupilot.xlsx", "validation": f"{stem}_validation.json", "result": "result.pkl"}
    (folder / files["workbook"]).write_bytes(result.excel_bytes)
    (folder / files["validation"]).write_text(validation_json(result), encoding="utf-8")
    blob = pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)
    (folder / files["result"]).write_bytes(blob)
    files["result_sha256"] = hashlib.sha256(blob).hexdigest()
    return files


def record_run(result: ProcessingResult, source: Optional[bytes] = None) -> Optional[str]:
    """Add a run to the history (and store its files when enabled). Returns the run id, or None when history is off."""
    if not SETTINGS.history_enabled:
        return None
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{result.file_hash[:8]}"
    while _runs_dir().joinpath(run_id).exists() or any(e.get("run_id") == run_id for e in load_history()):
        time.sleep(1)  # same file converted twice within a second: wait for a distinct timestamp
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{result.file_hash[:8]}"
    result.run_id = run_id  # set first, so the saved result knows which entry it belongs to
    entry = {"run_id": run_id, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), **_entry(result)}
    with _LOCK:
        if SETTINGS.history_files and source is not None:
            try:
                folder = run_dir(run_id)
                (folder / "source").mkdir(parents=True, exist_ok=True)
                (folder / "source" / result.file_name).write_bytes(source)
                entry["files"] = {"source": f"source/{result.file_name}", **_write_outputs(folder, result)}
            except OSError:
                log.exception("could not store the files of run %s", run_id)
        items = load_history()
        items.append(entry)
        dropped, items = items[:-MAX_ENTRIES], items[-MAX_ENTRIES:]
        for old in dropped:
            _remove_folder(old.get("run_id"))
        try:
            _save(items)
        except OSError:
            log.exception("could not write the history file")
    return run_id


def update_run(result: ProcessingResult) -> None:
    """Replace a run's outputs after the clean-up checklist rebuilt its workbook."""
    run_id = getattr(result, "run_id", None)
    if not run_id or not SETTINGS.history_enabled:
        return
    with _LOCK:
        items = load_history()
        entry = next((e for e in items if e.get("run_id") == run_id), None)
        if entry is None:
            return
        entry.update(_entry(result))
        entry["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if entry.get("files"):
            try:
                entry["files"].update(_write_outputs(run_dir(run_id), result))
            except OSError:
                log.exception("could not update the files of run %s", run_id)
        try:
            _save(items)
        except OSError:
            log.exception("could not write the history file")


def run_files(entry: dict[str, Any]) -> dict[str, Path]:
    """The stored files of a run that still exist on disk: {'source'|'workbook'|'validation'|'result': path}."""
    run_id, files = entry.get("run_id"), entry.get("files") or {}
    if not run_id:
        return {}
    try:
        folder = run_dir(run_id)
    except ValueError:
        return {}
    out = {}
    for kind in ("source", "workbook", "validation", "result"):
        rel = files.get(kind)
        if rel:
            p = (folder / rel).resolve()
            if p.is_file() and folder.resolve() in p.parents:
                out[kind] = p
    return out


def load_result(entry: dict[str, Any]) -> Optional[ProcessingResult]:
    """Reopen a stored run. The saved result is only loaded when it is the exact file this app wrote."""
    path = run_files(entry).get("result")
    if path is None:
        return None
    blob = path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != (entry.get("files") or {}).get("result_sha256"):
        log.warning("stored result of run %s does not match its checksum; not loaded", entry.get("run_id"))
        return None
    result = pickle.loads(blob)  # written by record_run/update_run above and checked against the stored digest
    return result if isinstance(result, ProcessingResult) else None


def _remove_folder(run_id: Optional[str]) -> None:
    if not run_id:
        return
    try:
        shutil.rmtree(run_dir(run_id), ignore_errors=True)
    except ValueError:
        pass


def delete_run(run_id: str) -> None:
    with _LOCK:
        _remove_folder(run_id)
        items = [e for e in load_history() if e.get("run_id") != run_id]
        try:
            _save(items)
        except OSError:
            log.exception("could not write the history file")


def clear_history() -> None:
    with _LOCK:
        try:
            _path().unlink(missing_ok=True)
        except OSError:
            pass
        shutil.rmtree(_runs_dir(), ignore_errors=True)


def storage_used() -> int:
    """Bytes used by stored run files."""
    root = _runs_dir()
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0


def lifetime_totals(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "documents": len(items),
        "pages": sum(int(i.get("pages", 0)) for i in items),
        "tables": sum(int(i.get("tables", 0)) for i in items),
        "rows": sum(int(i.get("rows", 0)) for i in items),
        "issues": sum(int(i.get("critical", 0)) + int(i.get("warnings", 0)) for i in items),
        "critical": sum(int(i.get("critical", 0)) for i in items),
    }
