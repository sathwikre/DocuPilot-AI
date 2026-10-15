"""Step B (scanned pages): rendering, image clean-up, OCR and ruled-grid detection.

OCR backend: RapidOCR running PaddleOCR models on ONNX Runtime (CPU, offline at inference time).

* Preferred: ``rapidocr`` 3.x with the PP-OCRv5 *English* mobile recogniser, which models spaces
  between words and returns word-level boxes. The model (~8 MB) is downloaded once from
  ModelScope on first use (or ahead of time with ``python scripts/download_models.py``).
* Fallback: ``rapidocr_onnxruntime`` 1.x with the PP-OCRv4 models bundled in the wheel (no
  download). Its recogniser often drops spaces in English text, so accuracy on descriptions is
  lower; numbers and dates are unaffected.

Both the library and the PaddleOCR models are Apache-2.0 licensed.
"""
from __future__ import annotations

import os
import threading
import unicodedata
from dataclasses import dataclass, field
from statistics import mean
from typing import Optional

import cv2
import pymupdf as fitz
import numpy as np

from config.settings import SETTINGS
from core.models import RawTable, Word
from core.table_detector import detect_word_tables, find_title, inside, table_from_grid, text_lines_outside
from utils.logging_config import get_logger

log = get_logger("ocr")

_engine = None
_engine_kind: Optional[str] = None  # "rapidocr-v3-en" | "rapidocr-onnxruntime-ch"
_engine_error: Optional[str] = None
_engine_lock = threading.Lock()
_ocr_lock = threading.Lock()  # ONNX sessions are shared; serialise calls from Streamlit threads


def _create_v3_engine():
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
    threads = max(1, min(8, os.cpu_count() or 4))
    engine = RapidOCR(params={
        "Global.log_level": "error",
        "Global.use_cls": False,  # page orientation is handled by DocuPilot itself
        "Det.limit_type": "max",
        "Det.limit_side_len": 1280,  # detection on a capped size; recognition uses full-res crops
        "Rec.lang_type": LangRec.EN,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.MOBILE,
        "EngineConfig.onnxruntime.intra_op_num_threads": threads,
    })
    # Force model loading/download now so failures surface here, not mid-document.
    engine(np.full((64, 256, 3), 255, dtype=np.uint8))
    return engine


def get_engine():
    """Lazily create the shared OCR engine, preferring the English PP-OCRv5 recogniser."""
    global _engine, _engine_kind, _engine_error
    with _engine_lock:
        if _engine is None and _engine_error is None:
            errors = []
            if os.environ.get("DOCUPILOT_OCR_BACKEND", "auto") in ("auto", "v3"):
                try:
                    _engine, _engine_kind = _create_v3_engine(), "rapidocr-v3-en"
                except Exception as exc:  # pragma: no cover - depends on environment / network
                    errors.append(f"rapidocr v3: {type(exc).__name__}: {exc}")
                    log.warning("English OCR model unavailable, falling back: %s", exc)
            if _engine is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR as LegacyOCR
                    _engine, _engine_kind = LegacyOCR(max_side_len=3600, print_verbose=False), "rapidocr-onnxruntime-ch"
                except Exception as exc:  # pragma: no cover
                    errors.append(f"rapidocr_onnxruntime: {type(exc).__name__}: {exc}")
            if _engine is None:
                _engine_error = "; ".join(errors) or "no OCR backend installed"
                log.warning("OCR engine unavailable: %s", _engine_error)
        return _engine


def ocr_installed_backend() -> str:
    """Describe the OCR backend that *would* be used, without loading any model (cheap, for UIs)."""
    if _engine_kind:
        return _engine_kind
    try:
        import importlib.util
        from pathlib import Path
        spec = importlib.util.find_spec("rapidocr")
        if spec and spec.origin:
            models = Path(spec.origin).parent / "models"
            if any("en_PP-OCRv5_rec" in f.name for f in models.glob("*")):
                return "rapidocr-v3-en (model cached)"
            return "rapidocr-v3-en (model downloads on first scanned page)"
        if importlib.util.find_spec("rapidocr_onnxruntime"):
            return "rapidocr-onnxruntime-ch"
    except Exception:
        pass
    return "not installed"


def ocr_available() -> bool:
    return get_engine() is not None


def ocr_backend() -> Optional[str]:
    get_engine()
    return _engine_kind


def ocr_status() -> str:
    get_engine()
    return f"ready ({_engine_kind})" if _engine is not None else f"unavailable ({_engine_error})"


@dataclass
class OCRPageResult:
    tables: list[RawTable]
    words: list[Word]
    text_lines: list[list[Word]]
    mean_confidence: Optional[float]
    low_conf_words: int
    ops: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- image helpers

def render_page(page: fitz.Page, dpi: int) -> np.ndarray:
    """Render a page (rotation applied) to an RGB numpy array."""
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).copy()


def estimate_skew(gray: np.ndarray) -> float:
    """Estimate small skew angle (degrees) with a projection-profile search."""
    h, w = gray.shape
    scale = 1000 / max(w, 1)
    small = cv2.resize(gray, (int(w * scale), int(h * scale))) if scale < 1 else gray
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    sh, sw = bw.shape
    center = (sw / 2, sh / 2)
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-3.0, 3.01, 0.2):
        m = cv2.getRotationMatrix2D(center, float(angle), 1.0)
        rot = cv2.warpAffine(bw, m, (sw, sh), flags=cv2.INTER_NEAREST, borderValue=0)
        score = float(np.var(rot.sum(axis=1)))
        if score > best_score:
            best_angle, best_score = float(angle), score
    # Refine around the best coarse angle.
    for angle in np.arange(best_angle - 0.2, best_angle + 0.21, 0.05):
        m = cv2.getRotationMatrix2D(center, float(angle), 1.0)
        rot = cv2.warpAffine(bw, m, (sw, sh), flags=cv2.INTER_NEAREST, borderValue=0)
        score = float(np.var(rot.sum(axis=1)))
        if score > best_score:
            best_angle, best_score = float(angle), score
    return round(best_angle, 2)


def rotate_image(img: np.ndarray, angle: float) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=(255, 255, 255))


# --------------------------------------------------------------------------- OCR

def _split_line_into_words(item, scale: float) -> list[Word]:
    """Turn one RapidOCR line result into words using character boxes when available."""
    box, text, score = item[0], item[1], float(item[2])
    text = unicodedata.normalize("NFKC", text)
    char_boxes = item[3] if len(item) > 3 else None
    chars = item[4] if len(item) > 4 else None
    words: list[Word] = []
    if char_boxes and chars and len(char_boxes) == len(chars):
        cur: list[tuple[str, list]] = []
        heights = [max(p[1] for p in b) - min(p[1] for p in b) for b in char_boxes]
        hmed = float(np.median(heights)) if heights else 10.0

        def flush():
            if cur:
                xs = [p[0] for _, b in cur for p in b]
                ys = [p[1] for _, b in cur for p in b]
                t = unicodedata.normalize("NFKC", "".join(c for c, _ in cur)).strip()
                if t:
                    words.append(Word(t, min(xs) * scale, min(ys) * scale, max(xs) * scale, max(ys) * scale, score))
                cur.clear()

        prev_x1 = None
        for ch, b in zip(chars, char_boxes):
            x0 = min(p[0] for p in b)
            if ch == " ":
                flush()
                prev_x1 = None
                continue
            if prev_x1 is not None and x0 - prev_x1 > 0.6 * hmed:
                flush()
            cur.append((ch, b))
            prev_x1 = max(p[0] for p in b)
        flush()
        if words:
            return words
    # Fallback: distribute words proportionally across the line box.
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    tokens = text.split()
    total = max(len(text), 1)
    pos = 0
    for tok in tokens:
        start = text.find(tok, pos)
        end = start + len(tok)
        pos = end
        words.append(Word(tok, (x0 + (x1 - x0) * start / total) * scale, y0 * scale,
                          (x0 + (x1 - x0) * end / total) * scale, y1 * scale, score))
    return words


def _box_word(text: str, box, score: float, scale: float) -> Optional[Word]:
    text = unicodedata.normalize("NFKC", str(text)).strip()
    if not text:
        return None
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return Word(text, min(xs) * scale, min(ys) * scale, max(xs) * scale, max(ys) * scale, float(score))


def ocr_image(img: np.ndarray, scale: float) -> tuple[list[Word], list[float]]:
    """OCR an RGB image; returns words (scaled to PDF points) and per-line confidences."""
    engine = get_engine()
    if engine is None:
        raise RuntimeError(f"OCR engine unavailable: {_engine_error}")
    words: list[Word] = []
    confs: list[float] = []
    with _ocr_lock:
        if _engine_kind == "rapidocr-v3-en":
            out = engine(img, return_word_box=True)
            if out.txts is None:
                return [], []
            word_results = out.word_results or ()
            for i, (box, txt, score) in enumerate(zip(out.boxes, out.txts, out.scores)):
                confs.append(float(score))
                line_words = word_results[i] if i < len(word_results) else ()
                got = [w for w in (_box_word(t, b, s, scale) for t, s, b in line_words) if w] if line_words else []
                if got:
                    words.extend(got)
                else:
                    words.extend(_split_line_into_words([box, txt, score], scale))
        else:
            result, _ = engine(img, return_word_box=True)
            for item in result or []:
                confs.append(float(item[2]))
                words.extend(_split_line_into_words(item, scale))
    return words, confs


def _looks_rotated(words: list[Word]) -> bool:
    multi = [w for w in words if len(w.text) >= 3]
    if len(multi) < 5:
        return False
    tall = sum(1 for w in multi if w.h > 1.5 * w.w)
    return tall / len(multi) > 0.6


# --------------------------------------------------------------------------- grid detection

def detect_grids(gray: np.ndarray) -> list[tuple[list[float], list[float]]]:
    """Find ruled table grids. Returns [(xs, ys)] in pixel coordinates."""
    h, w = gray.shape
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15)
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 30, 25), 1)))
    vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 60, 20))))
    mask = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((5, 5), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    grids = []
    for i in range(1, n):
        x, y, rw, rh, _area = stats[i]
        if rw < w * 0.2 or rh < 30:
            continue
        hl = _line_positions(horiz[y:y + rh, x:x + rw], axis=0, min_len=0.5 * rw)
        vl = _line_positions(vert[y:y + rh, x:x + rw], axis=1, min_len=0.5 * rh)
        if len(hl) >= 3 and len(vl) >= 3:
            grids.append(([x + v for v in vl], [y + v for v in hl]))
    return grids


def _line_positions(mask: np.ndarray, axis: int, min_len: float) -> list[float]:
    """Positions of long lines in a binary mask (axis=0 -> horizontal lines, rows)."""
    proj = (mask > 0).sum(axis=1 if axis == 0 else 0)
    idx = np.where(proj >= min_len)[0]
    if idx.size == 0:
        return []
    groups, cur = [], [idx[0]]
    for v in idx[1:]:
        if v - cur[-1] <= 3:
            cur.append(v)
        else:
            groups.append(cur)
            cur = [v]
    groups.append(cur)
    return [float(np.mean(g)) for g in groups]


# --------------------------------------------------------------------------- page pipeline

@dataclass
class OCRWords:
    """Output of the recognition step for one page (input to table reconstruction)."""

    page: int
    words: list[Word]
    grids: list[tuple[list[float], list[float]]]  # ruled grids in PDF points
    page_w: float
    page_h: float
    confidences: list[float]
    ops: list[str] = field(default_factory=list)

    @property
    def mean_confidence(self) -> Optional[float]:
        return round(mean(self.confidences), 4) if self.confidences else None


def ocr_page_words(page: fitz.Page, page_no: int, dpi: Optional[int] = None) -> OCRWords:
    """Render, deskew, orientation-correct and OCR one page; also locate ruled grids."""
    dpi = dpi or SETTINGS.ocr_dpi
    img = render_page(page, dpi)
    ops: list[str] = []
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    angle = estimate_skew(gray)
    if abs(angle) >= 0.25:
        img = rotate_image(img, angle)
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        ops.append(f"deskewed by {angle:+.2f} degrees")
    scale = 72.0 / dpi
    words, confs = ocr_image(img, scale)
    if _looks_rotated(words):
        best = (words, confs, img, 0)
        for rot_code, deg in ((cv2.ROTATE_90_CLOCKWISE, 90), (cv2.ROTATE_90_COUNTERCLOCKWISE, 270)):
            cand = cv2.rotate(img, rot_code)
            w2, c2 = ocr_image(cand, scale)
            if len(w2) and (mean(c2) * len(w2)) > ((mean(best[1]) if best[1] else 0) * len(best[0])) and not _looks_rotated(w2):
                best = (w2, c2, cand, deg)
        if best[3]:
            words, confs, img = best[0], best[1], best[2]
            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            ops.append(f"page content rotated {best[3]} degrees; corrected before OCR")
    grids = [([x * scale for x in xs], [y * scale for y in ys]) for xs, ys in detect_grids(gray)]
    return OCRWords(page=page_no, words=words, grids=grids, page_w=img.shape[1] * scale,
                    page_h=img.shape[0] * scale, confidences=confs, ops=ops)


def tables_from_ocr(ow: OCRWords) -> tuple[list[RawTable], list[list[Word]]]:
    """Reconstruct tables from OCR words: ruled grids first, then whitespace layout analysis."""
    tables: list[RawTable] = []
    for xs, ys in ow.grids:
        t = table_from_grid(ow.words, xs, ys, page=ow.page, page_w=ow.page_w, page_h=ow.page_h,
                            method="ocr-grid", ocr=True)
        if t is not None:
            tables.append(t)
    tables.extend(detect_word_tables(ow.words, page=ow.page, page_w=ow.page_w, page_h=ow.page_h,
                                     method="ocr-layout", ocr=True, exclude=[t.bbox for t in tables]))
    tables.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
    lines = text_lines_outside(ow.words, [t.bbox for t in tables])
    for i, t in enumerate(tables):
        t.index_on_page = i
        t.is_first_on_page = i == 0
        t.is_last_on_page = i == len(tables) - 1
        t.title = find_title(lines, t.bbox)
        t.warnings.extend(f"Image pre-processing: {o}" for o in ow.ops)
    return tables, lines


def ocr_page(page: fitz.Page, page_no: int, dpi: Optional[int] = None) -> OCRPageResult:
    """Convenience wrapper: OCR + table reconstruction for one page."""
    ow = ocr_page_words(page, page_no, dpi)
    tables, lines = tables_from_ocr(ow)
    low = sum(1 for w in ow.words if (w.conf or 1) < 0.8)
    return OCRPageResult(tables=tables, words=ow.words, text_lines=lines, mean_confidence=ow.mean_confidence,
                         low_conf_words=low, ops=ow.ops)
