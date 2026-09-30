"""
verify_scenarios.py — Прогоняет все тестовые xlsx/csv через wb_parser.analyze()
и проверяет, что каждая ожидаемая боль реально сработала.
"""
import sys, os
from wb_parser import analyze

EXPECTATIONS = {
    "test_1_competitor_attack.xlsx": {
        "must_contain": ["🚨", "Обнаружена аномалия", "проверьте регион", "COMPETE-ATK-01"],
        "must_NOT_contain": [],
        "label": "Боль 1: атака конкурентов",
    },
    "test_2_margin_eaten.xlsx": {
        "must_contain": ["📉", "Высокий процент отказов", "юнит-экономику", "REFUND-BAD-02"],
        "must_NOT_contain": [],
        "label": "Боль 2: расходы > 40%",
    },
    "test_3_fines_no_sales.xlsx": {
        "must_contain": ["⚠️", "штрафы/удержания без реализации", "тикет в поддержку", "LOST-ITEM-03"],
        "must_NOT_contain": [],
        "label": "Боль 3: штрафы без реализации",
    },
    "test_4_warehouse_overload.xlsx": {
        "must_contain": ["📦", "завис", "STUCK-WAREHOUSE-04"],
        "must_NOT_contain": [],
        "label": "Доп. сигнал: товар завис",
    },
    "test_5_all_healthy.xlsx": {
        "must_contain": ["✅ Итого"],
        "must_NOT_contain": ["🚨 Чистый убыток", "Обнаружена аномалия", "Высокий процент отказов", "штрафы/удержания без реализации"],
        "label": "Контрольный: всё здорово",
    },
    "test_6_false_positive_guard.xlsx": {
        "must_contain": [],  # SINGLE-RETURN попадёт в убыточные, но без Советника
        "must_NOT_contain": ["🚨 Обнаружена аномалия", "проверьте регион", "возможна атака конкурентов"],
        "label": "False-positive guard (1 поездка)",
    },
    "test_7_all_pains_combined.xlsx": {
        "must_contain": ["🚨", "📉", "⚠️", "ATK-FAKE-07", "MARGIN-DEAD-07", "LOST-PVZ-07"],
        "must_NOT_contain": [],
        "label": "Комбо: все 3 боли",
    },
    "test_quick_smoke.csv": {
        "must_contain": ["🚨", "CSV-ATK-99"],
        "must_NOT_contain": [],
        "label": "CSV smoke (Боль 1)",
    },
}

print("🧪 Запуск проверки тестовых отчётов через wb_parser.analyze()...\n")
passed = failed = 0
for filename, exp in EXPECTATIONS.items():
    if not os.path.exists(filename):
        print(f"❌ {filename} — НЕ НАЙДЕН")
        failed += 1
        continue
    try:
        report = analyze(filename)
    except Exception as e:
        print(f"❌ {filename} — ОШИБКА ПАРСЕРА: {e}")
        failed += 1
        continue
    ok = True
    for needle in exp["must_contain"]:
        if needle not in report:
            print(f"❌ {filename} ({exp['label']}) — НЕТ фразы: '{needle}'")
            ok = False
    for needle in exp["must_NOT_contain"]:
        if needle in report:
            print(f"❌ {filename} ({exp['label']}) — ЛИШНЯЯ фраза: '{needle}'")
            ok = False
    if ok:
        print(f"✅ {filename} ({exp['label']})")
        passed += 1
    else:
        failed += 1

print(f"\n{'='*60}")
print(f"ИТОГО: ✅ {passed} | ❌ {failed} | всего {passed+failed}")
print(f"{'='*60}")
sys.exit(0 if failed == 0 else 1)