"""UI workflow test: select a document, start extraction, open every page, download the workbook.

Uses Streamlit's AppTest harness (headless; no browser needed).
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")
PAGES = ["Overview", "Table Explorer", "Validation Center", "Export Center", "History", "Home"]


@pytest.fixture()
def isolated_history(tmp_path, monkeypatch):
    import config.settings as cs
    from dataclasses import replace
    import utils.history as h
    monkeypatch.setattr(h, "SETTINGS", replace(cs.SETTINGS, reports_dir=tmp_path))
    return tmp_path


def _start(at: AppTest, sample_label: str) -> AppTest:
    """A sample button converts straight away (like dropping a file and pressing Convert)."""
    at.run()
    assert not at.exception
    next(b for b in at.button if b.label.startswith(sample_label)).click().run()
    assert not at.exception
    return at


def test_empty_pages_show_empty_states(isolated_history):
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    for page in ["Overview", "Table Explorer", "Validation Center", "Export Center"]:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception
        assert any("No document processed yet" in m.value for m in at.markdown)


def test_upload_to_results_workflow(isolated_history):
    at = _start(AppTest.from_file(APP, default_timeout=300), "Complex report")
    assert at.session_state["nav"] == "Home"
    assert any("Your Excel file is ready" in m.value for m in at.markdown)
    res = at.session_state["results"][at.session_state["active"]]
    assert res.metrics()["critical"] == 1
    for page in PAGES:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, f"{page}: {at.exception}"
    # Results must not be recomputed on rerender: the same object stays in session state.
    at.sidebar.radio[0].set_value("Overview").run()
    assert at.session_state["results"][at.session_state["active"]] is res
    # History recorded the run's real metrics and kept its files.
    import json
    hist = json.loads((isolated_history / "history.json").read_text())
    assert hist[-1]["rows"] == 39 and hist[-1]["run_id"] == res.run_id
    assert set(hist[-1]["files"]) >= {"source", "workbook", "validation", "result"}


def test_history_page_reopens_a_stored_run(isolated_history):
    at = _start(AppTest.from_file(APP, default_timeout=300), "Complex report")
    first = at.session_state["results"][at.session_state["active"]]
    at.session_state["results"] = {}
    at.session_state["active"] = None
    at.sidebar.radio[0].set_value("History").run()
    assert not at.exception
    next(b for b in at.button if b.label == "Open results").click().run()
    assert not at.exception
    reopened = at.session_state["results"][at.session_state["active"]]
    assert reopened.run_id == first.run_id and reopened.excel_bytes == first.excel_bytes
    assert at.session_state["nav"] == "Home"


def test_export_page_offers_real_workbook(isolated_history):
    at = _start(AppTest.from_file(APP, default_timeout=300), "Complex report")
    at.sidebar.radio[0].set_value("Export Center").run()
    assert not at.exception
    res = at.session_state["results"][at.session_state["active"]]
    assert res.excel_bytes[:2] == b"PK"  # xlsx is a zip container


def test_checklist_apply_rebuilds_the_workbook(isolated_history):
    at = _start(AppTest.from_file(APP, default_timeout=300), "Complex report")
    h = at.session_state["active"]
    before = at.session_state["results"][h]
    apply = next(b for b in at.button if b.label == "Apply to the Excel file")
    assert apply.disabled, "nothing to apply until a box changes"
    next(c for c in at.checkbox if c.label.startswith("Total and sub-total rows")).check().run()
    next(b for b in at.button if b.label == "Apply to the Excel file").click().run()
    assert not at.exception
    after = at.session_state["results"][h]
    assert after is not before and "totals_separate" in after.options
    assert any("Excel file updated in" in m.value for m in at.markdown)


def test_keep_repeats_choice_reaches_the_engine_and_ocr_is_automatic(isolated_history):
    at = AppTest.from_file(APP, default_timeout=300)
    at.run()
    at.radio(key="opt_repeats").set_value("Keep them").run()
    next(b for b in at.button if b.label.startswith("Complex report")).click().run()
    assert not at.exception
    res = at.session_state["results"][at.session_state["active"]]
    assert "duplicates" not in res.options and res.ocr_mode == "auto"
