"""Robustness probe: re-scan the statement sample under harsher conditions and measure accuracy.

These variants were NOT used while developing the extractor, so they give a less optimistic view than
the main evaluation. Usage: python scripts/stress_probe.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.agent_orchestrator import process_document  # noqa: E402
from scripts.evaluate import evaluate_result  # noqa: E402
from scripts.generate_samples import build_statement, rasterize  # noqa: E402

VARIANTS = [
    ("150 dpi, noise 9, skew p2 0.6 deg (reference)", dict(dpi=150, noise=9.0, skew={2: 0.6})),
    ("120 dpi, noise 14, skew 1.2 deg all pages", dict(dpi=120, noise=14.0, skew={1: 1.2, 2: -1.2, 3: 1.2})),
    ("100 dpi, noise 18, skew 2.0 deg all pages", dict(dpi=100, noise=18.0, skew={1: 2.0, 2: -2.0, 3: 2.0})),
]

if __name__ == "__main__":
    digital, expected = build_statement(scanned=False)
    for name, kw in VARIANTS:
        pdf = rasterize(digital, seed=5, **kw)
        r = process_document(pdf, "stress.pdf")
        ev = evaluate_result(r, expected)
        crit = sum(1 for i in r.issues if i.severity == "critical")
        print(f"{name}: cells {ev['cell_accuracy']:.2%}, rows {ev['row_completeness']:.2%}, columns "
              f"{ev['column_alignment']:.2%}, tables {len(r.tables)}, critical findings {crit}, {r.elapsed:.0f}s")
