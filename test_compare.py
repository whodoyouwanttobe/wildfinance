"""Тесты сравнения отчётов (report_compare.py)."""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from report_compare import compare_metrics, compare_plain, sku_deltas


def _m(mp, profits, **totals):
    g = pd.DataFrame({"чистая_прибыль": pd.Series(profits, dtype=float)})
    base = {"marketplace": mp, "доход": 0.0, "логистика": 0.0, "чистая_прибыль": float(sum(profits.values()))}
    base.update(totals)
    base["grouped"] = g
    return base


def test_deltas_new_and_gone():
    old = _m("WB", {"a": 100, "b": 50})
    new = _m("WB", {"a": 80, "c": 30})
    df = sku_deltas(old, new)
    assert df.loc["a", "дельта"] == -20
    assert bool(df.loc["b", "пропал"]) and df.loc["b", "стало"] == 0
    assert bool(df.loc["c", "новый"])


def test_compare_html_shows_fallers_and_turned_loss():
    old = _m("WB", {"a": 1000, "b": 200}, доход=5000.0, логистика=300.0)
    new = _m("WB", {"a": 400, "b": -50}, доход=4000.0, логистика=600.0)
    out = compare_metrics(old, new)
    assert "Просели" in out
    assert "Ушли в минус" in out and "b" in out
    assert "1 000 → 400" in out
    assert "🔴 🚚 Логистика" in out  # рост расходов — красный


def test_expenses_compared_by_abs_for_ozon():
    old = _m("Ozon", {"x": 10}, доход=100.0, логистика=-50.0)
    new = _m("Ozon", {"x": 20}, доход=100.0, логистика=-30.0)
    out = compare_metrics(old, new)
    assert "🟢 🚚 Логистика: 50 → 30" in out


def test_different_marketplaces_rejected():
    with pytest.raises(ValueError, match="разных маркетплейсов"):
        compare_metrics(_m("WB", {"a": 1}), _m("Ozon", {"a": 1}))


def test_plain_summary_has_numbers():
    txt = compare_plain(_m("WB", {"a": 100}), _m("WB", {"a": 70}))
    assert "a: 100 → 70 (-30)" in txt
    assert "<b>" not in txt
