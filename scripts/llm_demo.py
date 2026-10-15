"""Measure what the optional local LLM adds: validation coverage on a table with non-standard headers.

The rules know 'Running Bal' and 'Dt', but not 'Amt Paid Out' / 'Amt Recd'. Without roles for those
columns the running-balance checks cannot run. The LLM only proposes roles for the unmapped headers
(names only, sent to localhost); all arithmetic stays deterministic.

Usage:  python scripts/llm_demo.py      (needs Ollama running with a supported model)
"""
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.data_normalizer import type_table  # noqa: E402
from core.models import DocField, LogicalTable, RowMeta  # noqa: E402
from core.validator import Validator  # noqa: E402
from models.local_llm import LocalLLM, get_llm_status  # noqa: E402

HEADER = ["Dt", "Narrative", "Amt Paid Out", "Amt Recd", "Running Bal"]
ROWS = [["01/03/2024", "Coffee", "4.50", "", "995.50"], ["02/03/2024", "Salary", "", "1,000.00", "1,995.50"],
        ["03/03/2024", "Rent", "500.00", "", "1,495.50"], ["04/03/2024", "Fuel", "45.00", "", "1,450.50"],
        ["05/03/2024", "Refund", "", "20.00", "1,470.50"],
        ["06/03/2024", "Books", "30.00", "", "1,450.50"]]  # deliberate error: correct balance is 1,440.50
OPENING = [DocField("opening_balance", "Opening balance", "1,000.00", Decimal("1000.00"), 1, "text")]


def run(mapper):
    t = LogicalTable("T1", None, [1], HEADER, list(HEADER), [None] * 5, [list(r) for r in ROWS],
                     [RowMeta(f"T1-R{i + 1}", 1, "pdf-layout") for i in range(len(ROWS))], ["pdf-layout"])
    type_table(t, llm_mapper=mapper)
    issues = Validator().validate([t], OPENING, "bank_statement")
    verified = [i for i in issues if i.check == "running_balance" and i.severity == "passed"]
    crit = [i for i in issues if i.severity == "critical"]
    return t, verified, crit


if __name__ == "__main__":
    t, verified, crit = run(None)
    print("Rules only  : roles", t.roles, "| running-balance checks passed:", [v.explanation for v in verified] or "none run",
          "| critical findings:", len(crit))
    st = get_llm_status()
    if not st.available:
        print("Local LLM not available:", st.error)
        sys.exit(0)
    llm = LocalLLM(model=st.selected)
    t0 = time.time()
    t, verified, crit = run(llm.map_headers)
    print(f"With {st.selected}: roles", t.roles, "| running-balance checks passed:",
          [v.explanation for v in verified] or "none run", f"| {time.time() - t0:.1f}s")
    print("Audit:", [op for op in t.header_ops if "LLM" in op])
    print("Critical findings:", [f"{c.row_id}: {c.explanation}" for c in crit])
