# DocuPilot AI — evaluation report

Generated 2026-10-15 11:12:31 by `scripts/evaluate.py` against the hand-checked ground truth in `samples/expected/`.

| Sample | Cell accuracy | Row completeness | Column alignment | Unwanted duplicates | Validation recall | Unexpected critical | Time (s) |
|---|---|---|---|---|---|---|---|
| sample_a_invoice.pdf | 100.00% | 100.00% | 100.00% | 0.00% | 2/2 | 0 | 0.1 |
| sample_b_statement_scanned.pdf | 99.38% | 100.00% | 100.00% | 0.00% | n/a (none planted) | 0 | 16.8 |
| sample_c_report.pdf | 100.00% | 100.00% | 100.00% | 0.00% | 1/1 | 0 | 0.6 |
| sample_d_invoice_photo.png | 100.00% | 100.00% | 100.00% | 0.00% | 2/2 | 0 | 7.7 |

**Overall:** cell accuracy 99.82% over 1128 expected cells; row completeness 100.00% over 113 rows; column alignment 100.00%; unwanted duplicate rows 0.00%; validation recall 100%.

## Cell mismatches (first 25 per table)

**sample_b_statement_scanned.pdf — T1**

- row 10, Ref No: expected `-`, got ``
- row 11, Ref No: expected `-`, got ``

