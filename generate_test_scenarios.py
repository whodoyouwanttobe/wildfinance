"""
generate_test_scenarios.py — Генератор тестовых отчётов WB.
Создаёт 7 .xlsx (каждый — отдельная боль) + 1 .csv smoke.
В каждом xlsx лист «README» с ожидаемым анализом.
"""

import pandas as pd


def base_row(artikul, obosnovanie, **kw):
    return {
        "Артикул поставщика": artikul,
        "Обоснование для оплаты": obosnovanie,
        "К перечислению Продавцу за реализованный товар": kw.get("dohod", 0),
        "Услуги по доставке товара покупателю": kw.get("logistika", 0),
        "Общая сумма штрафов": kw.get("shtrafy", 0),
        "Хранение": kw.get("hranenie", 0),
        "Операции при приёмке": kw.get("priemka", 0),
        "Удержания": kw.get("uderzhaniya", 0),
    }


def write_with_readme(path, rows, readme_lines):
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Отчёт", index=False)
        readme_df = pd.DataFrame({"ОЖИДАЕМЫЙ АНАЛИЗ": readme_lines})
        readme_df.to_excel(writer, sheet_name="README", index=False)
    print(f"✅ Создан: {path} ({len(rows)} строк)")


def s1():
    """СЦЕНАРИЙ 1: АТАКА КОНКУРЕНТОВ (Боль 1) — 0 продаж, 3 поездки."""
    rows = [
        base_row("COMPETE-ATK-01", "Логистика", logistika=800),
        base_row("COMPETE-ATK-01", "Логистика", logistika=850),
        base_row("COMPETE-ATK-01", "Логистика", logistika=820),
        base_row("HEALTHY-001", "Продажа", dohod=1200),
        base_row("HEALTHY-001", "Продажа", dohod=1100),
        base_row("HEALTHY-001", "Продажа", dohod=1300),
        base_row("HEALTHY-001", "Логистика", logistika=150),
    ]
    readme = [
        "🎯 СЦЕНАРИЙ 1: АТАКА КОНКУРЕНТОВ / ПОКАТУШКИ",
        "",
        "Артикул COMPETE-ATK-01 — 0 выручки, 3 логистические операции.",
        "",
        "✅ ОЖИДАЕМАЯ РЕАКЦИЯ:",
        "  • 🚨 Чистый убыток по артикулу: -2,470.00 руб.",
        "  • В ТОП-3 на 1-м месте.",
        "  • Советник 🚨 Обнаружена аномалия: товар ездит, но не выкупается.",
        "  • ➡️ Рекомендация: проверьте регион заказов...",
        "",
        "❌ FALSE-POSITIVE GUARD: HEALTHY-001 — НЕ должен триггерить.",
    ]
    write_with_readme("test_1_competitor_attack.xlsx", rows, readme)


def s2():
    """СЦЕНАРИЙ 2: ОТКАЗЫ СЪЕДАЮТ МАРЖУ (Боль 2) — расходы > 40% И в минусе."""
    rows = []
    for _ in range(5):
        rows.append(base_row("REFUND-BAD-02", "Продажа", dohod=1000))
        rows.append(base_row("REFUND-BAD-02", "Логистика", logistika=180))
    # Возвраты → двойная логистика
    for _ in range(5):
        rows.append(base_row("REFUND-BAD-02", "Логистика", logistika=180))
    # Крупные штрафы, чтобы уйти в минус (расходы > доход)
    rows.append(base_row("REFUND-BAD-02", "Штраф", shtrafy=2500))
    rows.append(base_row("REFUND-BAD-02", "Штраф", shtrafy=1500))
    rows.append(base_row("REFUND-BAD-02", "Удержание", uderzhaniya=300))
    # Здоровый артикул для контраста (расходы < 30%)
    for _ in range(3):
        rows.append(base_row("OK-LOW-RISK", "Продажа", dohod=2000))
        rows.append(base_row("OK-LOW-RISK", "Логистика", logistika=150))

    # Расчёт для README:
    # Доход: 5*1000 = 5000
    # Расходы: 5*180 + 5*180 + 2500 + 1500 + 300 = 1800+1800+4300 = 7900
    # Доля = 7900/5000 = 158%, Чистая = 5000-7900 = -2900
    readme = [
        "🎯 СЦЕНАРИЙ 2: ОТКАЗЫ СЪЕДАЮТ МАРЖУ (Боль 2) — В МИНУСЕ",
        "",
        "Артикул REFUND-BAD-02: 5 продаж × 1000 = 5000 руб. дохода.",
        "Расходы: логистика 3600 + штрафы 4000 + удержания 300 = 7900 руб. (158% чека!).",
        "Чистая прибыль: -2900 руб. (в минусе).",
        "",
        "✅ ОЖИДАЕМАЯ РЕАКЦИЯ:",
        "  • 📉 Высокий процент отказов. Логистика и штрафы съедают >100% от чека.",
        "  • ➡️ Поднимите цену на X руб. или выводите товар (X > 0!).",
        "  • НЕ должно быть '0.00 руб.'.",
        "",
        "❌ FALSE-POSITIVE GUARD: OK-LOW-RISK (расходы < 30%) — НЕ триггерит.",
    ]
    write_with_readme("test_2_margin_eaten.xlsx", rows, readme)
def s3():
    """СЦЕНАРИЙ 3: ШТРАФЫ БЕЗ РЕАЛИЗАЦИИ (Боль 3) — утери/подмены на ПВЗ."""
    rows = [
        base_row("LOST-ITEM-03", "Штраф", shtrafy=3000),
        base_row("LOST-ITEM-03", "Штраф", shtrafy=2000),
        base_row("LOST-ITEM-03", "Удержание", uderzhaniya=1500),
        base_row("LOST-ITEM-03", "Логистика", logistika=200),
    ]
    for _ in range(4):
        rows.append(base_row("OK-WITH-FINES", "Продажа", dohod=1500))
        rows.append(base_row("OK-WITH-FINES", "Штраф", shtrafy=200))
        rows.append(base_row("OK-WITH-FINES", "Логистика", logistika=150))

    readme = [
        "🎯 СЦЕНАРИЙ 3: ШТРАФЫ/УДЕРЖАНИЯ БЕЗ РЕАЛИЗАЦИИ (Боль 3)",
        "",
        "Артикул LOST-ITEM-03: 0 продаж, штрафы 5000 + удержания 1500 = 6500 руб.",
        "Это утери/подмены на ПВЗ.",
        "",
        "✅ ОЖИДАЕМАЯ РЕАКЦИЯ:",
        "  • 🚨 Чистый убыток по артикулу: -6,700.00 руб.",
        "  • ⚠️ Зафиксированы штрафы/удержания без реализации: 6,500.00 руб.",
        "  • ➡️ Скопируйте штрихкод и создайте тикет в поддержку ВБ.",
        "",
        "❌ FALSE-POSITIVE GUARD: OK-WITH-FINES (есть продажи) — НЕ триггерит.",
    ]
    write_with_readme("test_3_fines_no_sales.xlsx", rows, readme)


def s4():
    """СЦЕНАРИЙ 4: ТОВАР 'ЗАВИС' НА СКЛАДЕ (доп. сигнал) — большое хранение."""
    rows = [
        base_row("STUCK-WAREHOUSE-04", "Продажа", dohod=500),
        base_row("STUCK-WAREHOUSE-04", "Логистика", logistika=100),
        base_row("STUCK-WAREHOUSE-04", "Хранение", hranenie=4000),
        base_row("STUCK-WAREHOUSE-04", "Хранение", hranenie=4000),
        base_row("FAST-SELLER", "Продажа", dohod=2000),
        base_row("FAST-SELLER", "Продажа", dohod=1800),
        base_row("FAST-SELLER", "Логистика", logistika=200),
    ]
    readme = [
        "🎯 СЦЕНАРИЙ 4: ТОВАР 'ЗАВИС' НА СКЛАДЕ",
        "",
        "Артикул STUCK-WAREHOUSE-04: 1 продажа 500 руб., хранение 8000 руб.",
        "",
        "✅ ОЖИДАЕМАЯ РЕАКЦИЯ:",
        "  • 🚨 Чистый убыток по артикулу: -7,600.00 руб.",
        "  • 📦 Товар 'завис' на складе: хранение 8,000.00 руб. съедает маржу.",
        "  • ➡️ Добавьте артикул в акцию или снизьте цену.",
    ]
    write_with_readme("test_4_warehouse_overload.xlsx", rows, readme)
def s5():
    """СЦЕНАРИЙ 5: ВСЁ ЗДОРОВО — контрольный (ни одна боль не срабатывает)."""
    rows = []
    products = [
        ("HEALTHY-A", 1500, 180, 100),
        ("HEALTHY-B", 2200, 200, 150),
        ("HEALTHY-C", 1800, 170, 120),
        ("HEALTHY-D", 2500, 250, 180),
        ("HEALTHY-E", 1200, 130, 80),
    ]
    for art, price, log, fine in products:
        for _ in range(5):
            rows.append(base_row(art, "Продажа", dohod=price))
            rows.append(base_row(art, "Логистика", logistika=log))
        rows.append(base_row(art, "Штраф", shtrafy=fine))

    readme = [
        "🎯 СЦЕНАРИЙ 5: ВСЁ ЗДОРОВО (контрольный)",
        "",
        "Все 5 артикулов прибыльные. Расходы < 30% от чека.",
        "",
        "✅ ОЖИДАЕМАЯ РЕАКЦИЯ:",
        "  • ✅ Итого: <положительная сумма> руб. (без 🚨)",
        "  • ТОП-3 — 'мин. прибыль' (⚠️ но НЕ 'УБЫТОК').",
        "  • НЕТ инсайтов Советника (пустые строки).",
        "",
        "❌ ЧТО НЕ ДОЛЖНО БЫТЬ:",
        "  - Никаких 🚨 'Чистый убыток по артикулу'",
        "  - Никаких Советников с Болью 1/2/3",
    ]
    write_with_readme("test_5_all_healthy.xlsx", rows, readme)


def s6():
    """СЦЕНАРИЙ 6: FALSE-POSITIVE GUARD — 1 поездка, 0 продаж."""
    rows = [
        base_row("SINGLE-RETURN", "Логистика", logistika=400),
        base_row("NORMAL-PRODUCT", "Продажа", dohod=1000),
        base_row("NORMAL-PRODUCT", "Логистика", logistika=120),
    ]
    readme = [
        "🎯 СЦЕНАРИЙ 6: FALSE-POSITIVE GUARD (КРИТИЧНЫЙ!)",
        "",
        "SINGLE-RETURN: 1 логистическая операция, 0 продаж.",
        "Это одинокий дефолтный возврат, НЕ атака конкурентов.",
        "",
        "✅ ОЖИДАЕМАЯ РЕАКЦИЯ:",
        "  • SINGLE-RETURN попадёт в ТОП-3 (убыток -400 руб.).",
        "  • Под ним — НЕ должно быть '🚨 Обнаружена аномалия'.",
        "  • 1 поездка → Боль 1 НЕ триггерит (нужно >= 2).",
        "",
        "❌ ЧТО НЕ ДОЛЖНО БЫТЬ:",
        "  - 'проверьте регион заказов в кабинете ВБ'",
        "  - 'возможна атака конкурентов'",
    ]
    write_with_readme("test_6_false_positive_guard.xlsx", rows, readme)


def s7():
    """СЦЕНАРИЙ 7: КОМБО — все три боли в одном отчёте."""
    rows = []
    for _ in range(3):
        rows.append(base_row("ATK-FAKE-07", "Логистика", logistika=900))
    for _ in range(4):
        rows.append(base_row("MARGIN-DEAD-07", "Продажа", dohod=800))
        rows.append(base_row("MARGIN-DEAD-07", "Логистика", logistika=400))
    rows.append(base_row("LOST-PVZ-07", "Штраф", shtrafy=2500))
    rows.append(base_row("LOST-PVZ-07", "Удержание", uderzhaniya=800))
    for _ in range(3):
        rows.append(base_row("WINNER-07", "Продажа", dohod=2000))
        rows.append(base_row("WINNER-07", "Логистика", logistika=200))

    readme = [
        "🎯 СЦЕНАРИЙ 7: ВСЕ ТРИ БОЛИ В ОДНОМ ОТЧЁТЕ",
        "",
        "   - ATK-FAKE-07    → Боль 1 (атака конкурентов)",
        "   - MARGIN-DEAD-07 → Боль 2 (расходы > 40% чека)",
        "   - LOST-PVZ-07    → Боль 3 (штрафы без реализации)",
        "   - WINNER-07      → Здоровый (контраст)",
        "",
        "✅ В ТОП-3 убыточных должны быть 3 разных Советника:",
        "   1) 🚨 Обнаружена аномалия (Боль 1)",
        "   2) 📉 Высокий процент отказов (Боль 2)",
        "   3) ⚠️ Зафиксированы штрафы без реализации (Боль 3)",
    ]
    write_with_readme("test_7_all_pains_combined.xlsx", rows, readme)


def csv_smoke():
    """CSV smoke — самый простой кейс для быстрой проверки парсера."""
    rows = [
        base_row("CSV-ATK-99", "Логистика", logistika=750),
        base_row("CSV-ATK-99", "Логистика", logistika=750),
    ]
    path = "test_quick_smoke.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"✅ Создан: {path} ({len(rows)} строк)")


if __name__ == "__main__":
    print("🧪 Генерация тестовых отчётов Wildberries...\n")
    s1()
    s2()
    s3()
    s4()
    s5()
    s6()
    s7()
    csv_smoke()
    print("\n🎉 Готово! Загрузите эти файлы в @wildfinance_bot для проверки.")