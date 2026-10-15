"""Shared fixtures. Sample documents are generated in-memory so tests never depend on stale files."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent_orchestrator import process_document  # noqa: E402
from core.ocr_engine import ocr_available  # noqa: E402
from scripts import generate_samples as gen  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "ocr: test needs the OCR engine (slower)")


@pytest.fixture(scope="session")
def ocr_ok() -> bool:
    return ocr_available()


@pytest.fixture(scope="session")
def invoice():
    pdf, exp = gen.build_invoice()
    return pdf, exp


@pytest.fixture(scope="session")
def report_doc():
    return gen.build_report()


@pytest.fixture(scope="session")
def statement_scanned():
    return gen.build_statement(scanned=True)


@pytest.fixture(scope="session")
def invoice_result(invoice):
    return process_document(invoice[0], "invoice.pdf")


@pytest.fixture(scope="session")
def report_result(report_doc):
    return process_document(report_doc[0], "report.pdf")


@pytest.fixture(scope="session")
def statement_result(statement_scanned, ocr_ok):
    if not ocr_ok:
        pytest.skip("OCR engine not available")
    return process_document(statement_scanned[0], "statement.pdf")


@pytest.fixture(scope="session")
def expected_dir() -> Path:
    return ROOT / "samples" / "expected"


def load_expected(name: str) -> dict:
    return json.loads((ROOT / "samples" / "expected" / name).read_text(encoding="utf-8"))
