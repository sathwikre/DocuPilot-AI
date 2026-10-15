"""Centralised, environment-overridable configuration for DocuPilot AI."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Every value can be overridden with a DOCUPILOT_* env var."""

    max_pages: int = _env_int("DOCUPILOT_MAX_PAGES", 50)
    max_file_mb: int = _env_int("DOCUPILOT_MAX_FILE_MB", 60)
    ocr_dpi: int = _env_int("DOCUPILOT_OCR_DPI", 200)
    # A page with fewer extractable characters than this is treated as scanned.
    min_text_chars: int = 25
    # Fraction of page area covered by images above which a page is image-heavy.
    image_heavy_ratio: float = 0.45
    # Monetary tolerance used by the validator (absolute, in currency units).
    money_tolerance: str = "0.011"
    allowed_extensions: tuple[str, ...] = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
    # Local LLM (Ollama). Only localhost hosts are accepted unless explicitly allowed.
    ollama_host: str = os.environ.get("DOCUPILOT_OLLAMA_HOST", "http://127.0.0.1:11434")
    allow_remote_llm: bool = _env_bool("DOCUPILOT_ALLOW_REMOTE_LLM", False)
    preferred_llm_models: tuple[str, ...] = (
        "qwen2.5:3b-instruct", "qwen2.5:3b", "qwen2.5:1.5b", "llama3.2:3b",
        "gemma3:4b", "phi3:mini", "qwen2.5-coder:1.5b",
    )
    llm_timeout_s: int = _env_int("DOCUPILOT_LLM_TIMEOUT", 45)
    reports_dir: Path = field(default_factory=lambda: Path(os.environ.get("DOCUPILOT_REPORTS_DIR", PROJECT_ROOT / "reports")))
    logs_dir: Path = field(default_factory=lambda: Path(os.environ.get("DOCUPILOT_LOGS_DIR", PROJECT_ROOT / "logs")))
    history_enabled: bool = _env_bool("DOCUPILOT_HISTORY", True)


SETTINGS = Settings()
