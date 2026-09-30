"""
Реальные форматы выгрузок: «шапка» над таблицей, CSV с «;», числа «1 234,56»,
новые названия операций WB («Доставка», «… при отмене»), возвраты,
вознаграждение и компенсации Ozon со знаком.
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parser_dispatcher import analyze, compute, detect_marketplace  # noqa: E402

WB_COLS = [
    "№", "Тип документа", "Обоснование для оплаты", "Артикул поставщика",
    "К перечислению Продавцу за реализованный товар", "Услуги по доставке товара покупателю",
    "Общая сумма штрафов", "Корректировка Вознаграждения Вайлдберриз (ВВ)",
    "Виды доставок, штрафов и корректировок ВВ", "Хранение", "Удержания", "Операции при приёмке",
]


def _wb_rows():
    r = []
    def add(tip, osn, art, pay=0, log=0, fine=0, corr=0, vid="", stor=0, hold=0, acc=0):
        r.append([len(r) + 1, tip, osn, art, pay, log, fine, corr, vid, stor, hold, acc])
    # Нормальный товар: 2 продажи, 1 возврат
    add("Продажа", "Продажа", "GOOD-1", pay=1000)
    add("", "Доставка", "GOOD-1", log=80, vid="К клиенту при продаже")
    add("Продажа", "Продажа", "GOOD-1", pay=1000)
    add("", "Доставка", "GOOD-1", log=80, vid="К клиенту при продаже")
    add("Возврат", "Возврат", "GOOD-1", pay=1000)
    # Покатушки: 3 поездки при отмене, продаж нет
    for _ in range(3):
        add("", "Доставка", "ATK-1", log=150, vid="От клиента при отмене")
    # Прочее
    add("", "Штраф", "GOOD-1", fine=100, vid="Штраф за подмену")
    add("", "Удержания", "", hold=200, vid="WB Продвижение")
    add("", "Хранение", "", stor=50)
    add("", "Коррекция", "", corr=30, vid="Минимальный платёж за опции")
    return r


def _write_wb_xlsx(path):
    rows = _wb_rows()
    top = [["Детализация отчёта реализации № 123456"] + [None] * (len(WB_COLS) - 1),
           [None] * len(WB_COLS), WB_COLS]
    pd.DataFrame(top + rows).to_excel(path, header=False, index=False)


def test_wb_real_format_with_title_rows(tmp_path):
    path = str(tmp_path / "wb_detail.xlsx")
    _write_wb_xlsx(path)
    assert detect_marketplace(path) == "WB"
    m = compute(path)
    # доход: 2000 продаж − 1000 возврат
    assert m["доход"] == 1000
    # расходы: логистика 160+450, штраф 100, удержания 200+30 (корректировка ВВ), хранение 50
    assert m["логистика"] == 610
    assert m["удержания"] == 230
    assert round(m["чистая_прибыль"], 2) == 1000 - 610 - 100 - 230 - 50
    g = m["grouped"]
    assert g.loc["ATK-1", "поездок"] == 3 and g.loc["ATK-1", "отмен"] == 3
    out = analyze(path)
    assert "атака конкурентов" in out.lower()


def test_ozon_csv_semicolon_title_and_comma_numbers(tmp_path):
    path = tmp_path / "ozon.csv"
    lines = [
        "Отчёт по начислениям за период 01.09.2026 - 30.09.2026",
        "",
        "ID начисления;Дата начисления;Группа услуг;Тип начисления;SKU;Артикул;Цена продавца;Сумма итого, руб",
        "111-1;2026-09-10;Продажи;Выручка;1001;A-1;1 500,00;1 500,00",
        "111-1;2026-09-10;Продажи;Вознаграждение за продажу;1001;A-1;1 500,00;-225,00",
        "111-1;2026-09-10;Эквайринг;Эквайринг;1001;A-1;1 500,00;-22,50",
        "111-1;2026-09-10;Доставка;Обработка отправления Drop-off (СЦ);1001;A-1;1 500,00;-30,00",
        "222-2;2026-09-11;Возвраты;Возврат выручки;1001;A-1;1 500,00;-1 500,00",
        "333-3;2026-09-12;Компенсации;Компенсация за утерянный товар;1001;A-1;1 500,00;700,00",
        "01.09.26-30.09.26;2026-09-30;Продвижение;Трафареты;;;;-100,00",
    ]
    path.write_text("\n".join(lines), encoding="utf-8-sig")
    assert detect_marketplace(str(path)) == "Ozon"
    m = compute(str(path))
    assert m["доход"] == 0.0            # 1500 выручки − 1500 возврат
    assert m["эквайринг"] == -22.5
    # Прибыль = сумма всех строк со знаком (компенсация +700 уменьшает потери)
    assert round(m["чистая_прибыль"], 2) == round(1500 - 225 - 22.5 - 30 - 1500 + 700 - 100, 2)
    out = analyze(str(path))
    assert "Вознаграждение Ozon" in out and "Эквайринг" in out and "Drop-off" in out


def test_old_simple_wb_format_still_works(tmp_path):
    df = pd.DataFrame({
        "Артикул поставщика": ["X", "X", "X"],
        "Обоснование для оплаты": ["Логистика", "Логистика", "Логистика"],
        "К перечислению Продавцу за реализованный товар": [0, 0, 0],
        "Услуги по доставке товара покупателю": [100, 100, 100],
    })
    path = str(tmp_path / "old.xlsx")
    df.to_excel(path, index=False)
    m = compute(path)
    assert m["grouped"].loc["X", "поездок"] == 3
