"""Pre-fetch and verify the OCR model so the first scanned page does not wait for a download.

Usage:  python scripts/download_models.py
Downloads the PP-OCRv5 English recogniser (~8 MB, Apache-2.0) from ModelScope via rapidocr. Without
network access DocuPilot falls back to the PP-OCRv4 models bundled in rapidocr-onnxruntime.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ocr_engine import ocr_status  # noqa: E402

if __name__ == "__main__":
    status = ocr_status()
    print("OCR engine:", status)
    sys.exit(0 if status.startswith("ready") else 1)
