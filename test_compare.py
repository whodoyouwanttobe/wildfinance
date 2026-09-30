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


# ─── WB против Ozon ─────────────────────────────────────────────────────────

from report_compare import compare_marketplaces  # noqa: E402


def _wb(profits_units):
    g = pd.DataFrame({
        "чистая_прибыль": {k: v[0] for k, v in profits_units.items()},
        "шт": {k: v[1] for k, v in profits_units.items()},
        "выручка_брутто": {k: v[2] for k, v in profits_units.items()},
    })
    return {"marketplace": "WB", "доход": 800.0, "логистика": 100.0, "штрафы": 0.0, "хранение": 20.0,
            "приёмка": 0.0, "удержания": 30.0, "чистая_прибыль": float(g["чистая_прибыль"].sum()), "grouped": g}


def _oz(profits_units):
    g = pd.DataFrame({
        "чистая_прибыль": {f"SKU:{k}": v[0] for k, v in profits_units.items()},
        "шт": {f"SKU:{k}": v[1] for k, v in profits_units.items()},
        "выручка_брутто": {f"SKU:{k}": v[2] for k, v in profits_units.items()},
        "артикул": {f"SKU:{k}": k for k in profits_units},
    })
    return {"marketplace": "Ozon", "доход": 1000.0, "комиссия": -150.0, "эквайринг": -15.0,
            "логистика": -60.0, "хранение": 0.0, "штрафы": 0.0, "партнёры": -20.0, "прочее": 0.0,
            "чистая_прибыль": float(g["чистая_прибыль"].sum()), "grouped": g}


def test_wb_vs_ozon_summary_and_common_items():
    wb = _wb({"JEANS": (650, 2, 1000)})
    oz = _oz({"JEANS": (755, 2, 1000)})
    out = compare_marketplaces(oz, wb)  # порядок не важен
    assert "WB против OZON" in out
    assert "На Ozon с каждых 100 ₽ выручки остаётся на 10 ₽ больше" in out
    assert "JEANS: WB 325 ₽ · Ozon 378 ₽ → выгоднее <b>Ozon</b>" in out


def test_wb_vs_ozon_requires_both():
    with pytest.raises(ValueError):
        compare_marketplaces(_wb({"A": (1, 1, 1)}), _wb({"A": (1, 1, 1)}))
