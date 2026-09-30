"""Тесты ABC-анализа (abc_analysis.py) и его встраивания в отчёты WB/Ozon."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from abc_analysis import abc_classify, render_abc

HERE = os.path.dirname(os.path.abspath(__file__))


def test_classic_pareto():
    profit = pd.Series({"a": 700, "b": 150, "c": 100, "d": 30, "e": 20})
    cls = abc_classify(profit)["класс"].to_dict()
    assert cls == {"a": "A", "b": "A", "c": "B", "d": "C", "e": "C"}


def test_single_leader_stays_in_a():
    """Лидер с 95% прибыли — это A, а не B."""
    cls = abc_classify(pd.Series({"x": 950, "y": 50}))["класс"]
    assert cls["x"] == "A"
    assert cls["y"] == "C"


def test_losses_are_separate():
    df = abc_classify(pd.Series({"good": 1000, "bad": -300, "zero": 0}))
    assert df.loc["bad", "класс"] == "loss"
    assert df.loc["zero", "класс"] == "zero"
    assert df.loc["good", "класс"] == "A"
    # Доли считаются только от положительной прибыли
    assert abs(df.loc["good", "доля"] - 1.0) < 1e-9


def test_empty_sku_excluded():
    df = abc_classify(pd.Series({"": -500, "a": 100, "b": 50}))
    assert "" not in df.index


def test_all_loss_no_crash():
    lines = render_abc(pd.Series({"a": -10, "b": -20}))
    text = "\n".join(lines)
    assert "Убыточные" in text
    assert "Группа A" not in text


def test_render_escapes_html_and_strips_ozon_prefix():
    text = "\n".join(render_abc(pd.Series({"SKU:<x>": 100, "ART:y": 50})))
    assert "&lt;x&gt;" in text
    assert "SKU:" not in text and "ART:" not in text


def test_too_few_items_returns_nothing():
    assert render_abc(pd.Series({"a": 100})) == []


def test_wb_report_contains_abc():
    from wb_parser import analyze
    out = analyze(os.path.join(HERE, "wb_real_report.xlsx"))
    assert "ABC-АНАЛИЗ" in out
    assert "Группа A" in out


def test_ozon_report_contains_abc():
    from ozon_parser import analyze
    out = analyze(os.path.join(HERE, "ozon_real_report.xlsx"))
    assert "ABC-АНАЛИЗ" in out
