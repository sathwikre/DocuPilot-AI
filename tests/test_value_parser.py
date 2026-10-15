from datetime import date
from decimal import Decimal

import pytest

from core.value_parser import cell_kind, infer_date_order, ocr_repair_number, parse_date, parse_number


@pytest.mark.parametrize("text,value", [
    ("1,234.56", "1234.56"), ("-1,234.56", "-1234.56"), ("(1,250.00)", "-1250.00"), ("$ 99.50", "99.50"),
    ("€1.234,56", "1234.56"), ("1,23,456.78", "123456.78"), ("12-", "-12"), ("0.42", "0.42"), ("−3.5", "-3.5"),
    ("-0.01", "-0.01"), ("(9.0)", "-9.0"), ("7", "7"),
])
def test_numbers_including_negatives_and_decimals(text, value):
    p = parse_number(text)
    assert p is not None and p.value == Decimal(value)


def test_decimal_precision_is_preserved():
    assert parse_number("10.10").decimals == 2
    assert str(parse_number("10.10").value) == "10.10"


def test_percent_and_drcr_markers():
    p = parse_number("18%")
    assert p.is_percent and p.value == Decimal(18)
    p = parse_number("250.00 CR")
    assert p.drcr == "CR" and p.value == Decimal("250.00")  # sign is never changed silently


@pytest.mark.parametrize("text", ["abc", "INV-0042", "", None, "1 234 567 890 123 456 789 000 111 222 333", "12.34.56"])
def test_non_numbers(text):
    assert parse_number(text) is None


def test_dates():
    assert parse_date("2024-01-05") == date(2024, 1, 5)
    assert parse_date("05/01/2024", "DMY") == date(2024, 1, 5)
    assert parse_date("05/01/2024", "MDY") == date(2024, 5, 1)
    assert parse_date("Jan 5, 2024") == date(2024, 1, 5)
    assert parse_date("18 Mar 2024") == date(2024, 3, 18)
    assert parse_date("31/02/2024") is None


def test_date_order_inference():
    assert infer_date_order(["13/03/2024", "01/03/2024"]) == "DMY"
    assert infer_date_order(["03/13/2024", "03/01/2024"]) == "MDY"
    assert infer_date_order(["01/02/2024", "03/04/2024"]) is None


def test_ocr_repair_only_touches_digit_neighbourhoods():
    parsed, repaired = ocr_repair_number("1O0.00")
    assert repaired == "100.00" and parsed.value == Decimal("100.00")
    assert ocr_repair_number("USD") is None
    assert ocr_repair_number("Total") is None


def test_cell_kind():
    assert cell_kind("") == "empty"
    assert cell_kind("1,000") == "number"
    assert cell_kind("2024-03-01") == "date"
    assert cell_kind("Widget") == "text"
