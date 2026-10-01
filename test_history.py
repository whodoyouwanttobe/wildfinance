"""Тесты истории продаж и матрицы ABC × XYZ (history.py)."""

import os
from datetime import date, timedelta

import pandas as pd
import pytest

os.environ.setdefault("BOT_TOKEN", "123456:FAKE-TOKEN-FOR-TESTS")

import database  # noqa: E402
import history as H  # noqa: E402
import parser_dispatcher as PD  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _metrics(rows: list[tuple], mp="WB"):
    """rows: (дата, артикул, шт, выплата)"""
    df = pd.DataFrame(rows, columns=["дата", "артикул", "шт", "выплата"])
    df["выручка"] = df["выплата"]
    return {"marketplace": mp, "rows": df, "grouped": None}


def _week(monday: date, sales: dict[str, int], price=100.0, mp="WB"):
    """Недельный отчёт: продажи распределены по дням пн–вс."""
    rows = []
    for art, qty in sales.items():
        for i in range(qty):
            rows.append(((monday + timedelta(days=i % 7)).strftime("%d.%m.%Y"), art, 1, price))
        if qty == 0:
            rows.append((monday.strftime("%d.%m.%Y"), art, 0, -10.0))   # только логистика
    rows.append(((monday + timedelta(days=6)).strftime("%d.%m.%Y"), "", 0, -50.0))  # хранение без артикула
    return _metrics(rows, mp)


MON = date(2026, 8, 3)   # понедельник


@pytest.fixture
def dbp(tmp_path):
    return str(tmp_path / "h.db")


def test_parse_dates_formats():
    s = pd.Series(["2025-09-13", "13.09.2025", "2025-09-13 10:15:00", "", None, "мусор"])
    out = H.parse_dates(s)
    assert out.iloc[0] == out.iloc[1] == out.iloc[2] == pd.Timestamp("2025-09-13")
    assert out.iloc[3:].isna().all()


def test_weekly_report_snaps_to_monday_sunday():
    # продажи только со среды по субботу + хвост с прошлой недели
    m = _metrics([("12.08.2026", "A", 1, 100), ("15.08.2026", "A", 1, 100),
                  ("13.08.2026", "B", 1, 100), ("07.08.2026", "A", 1, 100)])
    daily, start, end, dated = H.daily_from_metrics(m)
    assert dated and start == date(2026, 8, 10) and end == date(2026, 8, 16)
    assert daily["день"].min() == "2026-08-10"   # хвост прижат к началу недели


def test_monthly_report_keeps_range_and_clamps_old_rows():
    rows = [((date(2026, 9, 1) + timedelta(days=i)).strftime("%Y-%m-%d"), "A", 1, 10) for i in range(30)]
    rows.append(("2026-03-01", "A", 0, -500))     # корректировка задним числом
    daily, start, end, _ = H.daily_from_metrics(_metrics(rows, "Ozon"))
    assert end == date(2026, 9, 30)
    assert start == end - timedelta(days=H.MAX_REPORT_DAYS)
    assert daily["день"].min() >= start.isoformat()


def test_no_dates_means_previous_week():
    m = _metrics([(None, "A", 2, 100)])
    daily, start, end, dated = H.daily_from_metrics(m, today=date(2026, 10, 1))   # четверг
    assert not dated
    assert start == date(2026, 9, 21) and end == date(2026, 9, 27)


def test_complete_weeks():
    ranges = [(date(2026, 8, 3), date(2026, 8, 9)),        # полная
              (date(2026, 8, 12), date(2026, 8, 16)),       # неполная
              (date(2026, 8, 17), date(2026, 9, 2))]        # 2 полные + хвост
    assert H.complete_weeks(ranges) == [date(2026, 8, 3), date(2026, 8, 17), date(2026, 8, 24)]


def test_reupload_does_not_double(dbp):
    m = _week(MON, {"A": 5})
    H.save_report(1, m, db_path=dbp)
    H.save_report(1, m, db_path=dbp)
    conn = database.get_connection(dbp)
    total = conn.execute("SELECT SUM(qty) FROM sales_daily WHERE user_id = 1").fetchone()[0]
    conn.close()
    assert total == 5


def test_not_ready_until_min_weeks(dbp):
    for k in range(H.MIN_WEEKS - 1):
        st = H.save_report(1, _week(MON + timedelta(weeks=k), {"A": 5}), db_path=dbp)
    assert st["weeks"] == H.MIN_WEEKS - 1 and not st["ready"]
    res = H.compute_matrix(1, "WB", db_path=dbp)
    assert not res["ready"]
    assert f"{H.MIN_WEEKS - 1} из {H.MIN_WEEKS}" in H.render(res)


def test_matrix_classes(dbp):
    pattern = {
        "STAR": [20, 21, 19, 20, 20],    # много денег, ровно → AX
        "WAVE": [2, 10, 0, 12, 1],        # хаос → Z
        "SOCK": [3, 3, 3, 3, 3],          # мало денег, ровно → CX
        "DEAD": [0, 0, 0, 0, 0],          # без продаж
    }
    for k in range(5):
        H.save_report(1, _week(MON + timedelta(weeks=k), {a: q[k] for a, q in pattern.items()}), db_path=dbp)
    res = H.compute_matrix(1, "WB", db_path=dbp)
    assert res["ready"] and len(res["weeks"]) == 5
    t = res["table"]
    assert (t.loc["STAR", "abc"], t.loc["STAR", "xyz"]) == ("A", "X")
    assert t.loc["WAVE", "xyz"] == "Z"
    assert t.loc["SOCK", "xyz"] == "X"
    assert t.loc["DEAD", "xyz"] == "—"
    assert "" not in t.index                      # общие расходы без артикула не в матрице
    text = H.render(res)
    assert "<pre>" in text and "AX" in text and "STAR" in text
    assert "Убыточные" in text and "DEAD" in text  # только логистика → убыток


def test_marketplaces_separate(dbp):
    H.save_report(1, _week(MON, {"A": 1}), db_path=dbp)
    H.save_report(1, _week(MON, {"A": 1}, mp="Ozon"), db_path=dbp)
    assert set(H.marketplaces_with_history(1, db_path=dbp)) == {"WB", "Ozon"}
    assert H.status(1, "Ozon", db_path=dbp)["weeks"] == 1


def test_real_files_have_rows():
    for f, mp in (("wb_real_report.xlsx", "WB"), ("ozon_real_report.xlsx", "Ozon")):
        m = PD.compute(os.path.join(HERE, f))
        assert m["marketplace"] == mp
        daily, start, end, dated = H.daily_from_metrics(m)
        assert dated and not daily.empty and start <= end
        # Сумма выплат по истории = итог отчёта без общих расходов (строк без артикула)
        g = m["grouped"]
        expected = g.loc[[i for i in g.index if str(i).strip()], "чистая_прибыль"].sum()
        assert daily["выплата"].sum() == pytest.approx(expected, abs=0.05)


def test_delete_user_data_removes_history(dbp):
    H.save_report(3, _week(MON, {"A": 1}), db_path=dbp)
    database.delete_user_data(3, db_path=dbp)
    assert H.marketplaces_with_history(3, db_path=dbp) == []


# ─── Бот ────────────────────────────────────────────────────────────────────

import bot as B  # noqa: E402
from test_bot_features import db, _msg, _cb  # noqa: E402,F401


@pytest.mark.asyncio
async def test_xyz_command_empty_and_ready(db):
    m = _msg(50, "/xyz")
    await B.cmd_xyz(m)
    assert "минимум" in m.answer.call_args.args[0]

    for k in range(H.MIN_WEEKS):
        H.save_report(50, _week(MON + timedelta(weeks=k), {"A": 5 + k, "B": 1}))
    c = _cb(50, "xyz")
    await B.callback_xyz(c)
    assert "ABC × XYZ — WB" in c.message.answer.call_args_list[0].args[0]


def test_history_line():
    assert B._history_line(None) == ""
    line = B._history_line({"weeks": 1, "ready": False, "start": MON, "end": MON + timedelta(days=6), "dated": True})
    assert "1 из" in line and "03.08–09.08" in line
    line = B._history_line({"weeks": 5, "ready": True, "start": MON, "end": MON, "dated": False})
    assert "/xyz" in line and "прошлой неделей" in line
