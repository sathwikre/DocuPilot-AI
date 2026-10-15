"""OCR path, image uploads, end-to-end accuracy against ground truth, optional local LLM."""
import pytest

from core.agent_orchestrator import STAGES, process_document
from scripts.evaluate import evaluate_result
from tests.conftest import load_expected


@pytest.mark.ocr
def test_scanned_statement_accuracy(statement_result, statement_scanned):
    ev = evaluate_result(statement_result, statement_scanned[1])
    assert ev["row_completeness"] == 1.0
    assert ev["column_alignment"] == 1.0
    assert ev["cell_accuracy"] >= 0.98
    assert ev["duplicate_rate"] == 0.0
    assert all(p.ocr_used for p in statement_result.page_reports)
    assert any("deskewed" in w for w in statement_result.page_reports[1].warnings)


@pytest.mark.ocr
def test_image_upload_uses_ocr_grid(ocr_ok, invoice):
    if not ocr_ok:
        pytest.skip("OCR engine not available")
    import pymupdf as fitz
    page = fitz.open(stream=invoice[0], filetype="pdf")[0]
    png = page.get_pixmap(dpi=170, colorspace=fitz.csGRAY).tobytes("png")
    r = process_document(png, "photo.png")
    assert r.doc_info.source_type == "image"
    assert "ocr-grid" in r.tables[0].methods
    ev = evaluate_result(r, invoice[1])
    assert ev["cell_accuracy"] >= 0.97 and ev["validation_recall"] == 1.0


@pytest.mark.parametrize("name,expected", [("invoice", "sample_a_invoice.json"), ("report", "sample_c_report.json")])
def test_digital_samples_match_ground_truth(name, expected, invoice_result, report_result):
    result = invoice_result if name == "invoice" else report_result
    ev = evaluate_result(result, load_expected(expected))
    assert ev["cell_accuracy"] == 1.0 and ev["row_completeness"] == 1.0 and ev["column_alignment"] == 1.0
    assert ev["validation_recall"] == 1.0
    assert ev["unexpected_critical"] == []


def test_progress_events_are_real_and_ordered(invoice):
    events = []
    process_document(invoice[0], "inv.pdf", progress=events.append)
    finished = [e.stage_index for e in events if e.finished]
    assert finished == list(range(len(STAGES)))
    page_events = [e for e in events if e.stage_index == 2 and not e.finished and e.total_units]
    assert page_events[-1].done_units == page_events[-1].total_units == 1


def test_doc_types(invoice_result, report_result):
    assert invoice_result.doc_type == "invoice"
    assert report_result.doc_type == "financial_report"


def test_llm_unavailable_does_not_break_pipeline(monkeypatch, invoice):
    import config.settings as cs
    from dataclasses import replace
    monkeypatch.setattr(cs, "SETTINGS", replace(cs.SETTINGS, ollama_host="http://127.0.0.1:9"))
    import models.local_llm as ll
    monkeypatch.setattr(ll, "SETTINGS", cs.SETTINGS)
    r = process_document(invoice[0], "inv.pdf", use_llm=True)
    assert r.tables and not r.llm_used
    assert any("unavailable" in n for n in r.llm_notes)


def test_llm_refuses_remote_host():
    from models.local_llm import LocalLLM
    with pytest.raises(ValueError):
        LocalLLM(host="https://api.example.com")


def test_llm_answers_are_constrained(monkeypatch):
    from models.local_llm import LocalLLM
    llm = LocalLLM(model="fake")
    monkeypatch.setattr(llm, "generate_json", lambda prompt, schema=None: {"Amt Recd": "credit", "Weird": "delete_all_rows", "X": 5})
    out = llm.map_headers(["Amt Recd", "Weird", "X"], ["credit", "debit"])
    assert out == {"Amt Recd": "credit"}
