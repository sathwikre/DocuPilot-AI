"""Compact Altair charts styled for the dark theme. Every chart is drawn from real result data."""
from __future__ import annotations

from collections import Counter

import altair as alt
import pandas as pd

from core.models import ProcessingResult
from ui.components import CHECK_LABELS

SEV_COLORS = {"Critical": "#9b2c2c", "Warning": "#f59e0b", "Informational": "#3b6e8f", "Passed": "#2b7a73"}
SEV_ORDER = list(SEV_COLORS)
_SEV_NAME = {"critical": "Critical", "warning": "Warning", "info": "Informational", "passed": "Passed"}


def _style(chart: alt.Chart, height: int = 210) -> alt.Chart:
    return (chart.properties(height=height, background="transparent")
            .configure_view(strokeWidth=0)
            .configure_axis(labelColor="#8a7462", titleColor="#a08a76", gridColor="#ece2d1",
                            domainColor="#e3d6c1", tickColor="#e3d6c1",
                            labelFont="Segoe UI", titleFont="Segoe UI", labelFontSize=11, titleFontSize=11)
            .configure_legend(labelColor="#5a4738", titleColor="#a08a76", labelFont="Segoe UI", orient="bottom")
            .configure_title(color="#2a1f17"))


def severity_chart(result: ProcessingResult) -> alt.Chart:
    c = Counter(_SEV_NAME[i.severity] for i in result.issues)
    df = pd.DataFrame({"Severity": SEV_ORDER, "Findings": [c.get(s, 0) for s in SEV_ORDER]})
    base = alt.Chart(df).encode(
        y=alt.Y("Severity:N", sort=SEV_ORDER, title=None),
        x=alt.X("Findings:Q", title=None, axis=alt.Axis(tickMinStep=1)),
    )
    bars = base.mark_bar(cornerRadiusEnd=6, height=18).encode(
        color=alt.Color("Severity:N", scale=alt.Scale(domain=SEV_ORDER, range=list(SEV_COLORS.values())), legend=None),
        tooltip=["Severity", "Findings"])
    text = base.mark_text(align="left", dx=6, color="#2a1f17", fontSize=12, fontWeight="bold").encode(text="Findings:Q")
    return _style(bars + text, 190)


def rows_by_page_chart(result: ProcessingResult) -> alt.Chart:
    rows = []
    for t in result.tables:
        cnt = Counter(m.page for m in t.row_meta if m.kind == "data")
        for p, n in cnt.items():
            rows.append({"Page": p, "Rows": n, "Table": t.table_id})
    df = pd.DataFrame(rows) if rows else pd.DataFrame({"Page": [], "Rows": [], "Table": []})
    chart = alt.Chart(df).mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5).encode(
        x=alt.X("Page:O", title="Source page", axis=alt.Axis(labelAngle=0)),
        y=alt.Y("sum(Rows):Q", title=None),
        color=alt.Color("Table:N", scale=alt.Scale(range=["#ea580c", "#2b7a73", "#fb923c", "#9a3412", "#3b6e8f", "#fdba74"]),
                        legend=alt.Legend(title=None, orient="bottom")),
        tooltip=["Table", "Page", "Rows"])
    return _style(chart, 210)


def checks_chart(result: ProcessingResult) -> alt.Chart:
    rows = [{"Check": CHECK_LABELS.get(i.check, i.check), "Severity": _SEV_NAME[i.severity]} for i in result.issues]
    df = pd.DataFrame(rows) if rows else pd.DataFrame({"Check": [], "Severity": []})
    chart = alt.Chart(df).mark_bar(cornerRadiusEnd=4, height=14).encode(
        y=alt.Y("Check:N", title=None, sort="-x"),
        x=alt.X("count():Q", title=None, axis=alt.Axis(tickMinStep=1)),
        color=alt.Color("Severity:N", scale=alt.Scale(domain=SEV_ORDER, range=list(SEV_COLORS.values())),
                        legend=alt.Legend(title=None, orient="bottom")),
        tooltip=["Check", "Severity", "count()"])
    n = max(len(df["Check"].unique()) if len(df) else 1, 1)
    return _style(chart, max(160, 26 * n))


def stage_chart(result: ProcessingResult) -> alt.Chart:
    df = pd.DataFrame({"Stage": list(result.stage_timings), "Seconds": list(result.stage_timings.values())})
    chart = alt.Chart(df).mark_bar(cornerRadiusEnd=4, height=14, color="#ea580c").encode(
        y=alt.Y("Stage:N", sort=None, title=None), x=alt.X("Seconds:Q", title="seconds"),
        tooltip=["Stage", alt.Tooltip("Seconds:Q", format=".2f")])
    return _style(chart, 200)
