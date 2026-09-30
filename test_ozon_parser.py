"""
test_ozon_parser.py — Тесты парсера Ozon-отчётов.

Проверяем:
  - Базовое чтение + расчёт итогов
  - Обнаружение покатушек
  - Категоризацию (логистика, штрафы, эквайринг, партнёры)
  - Граничные случаи (пустой отчёт, отсутствие SKU)
  - Интеграцию с parser_dispatcher
"""

import os
import sys
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ozon_parser import (
    analyze,
    categorize,
    fmt,
    fmt_expense,
    generate_insight_ozon,
    load_report,
    detect_column_mapping,
)


# ─── Фикстуры ────────────────────────────────────────────────────────────────

@pytest.fixture
def ozon_sample_csv(tmp_path):
    """Минимальный валидный Ozon-отчёт с известной математикой."""
    rows = [
        ["acc_a_1", "Продажи",   "Продажа",        1000.0, "2025-09-10", "SKU-A"],
        ["acc_a_2", "Эквайринг", "Эквайринг",       -15.0, "2025-09-10", "SKU-A"],
        ["acc_a_3", "Логистика", "Последняя миля",  -50.0, "2025-09-10", "SKU-A"],
        ["acc_b_1", "Продажи",   "Продажа",         200.0, "2025-09-11", "SKU-B"],
        ["acc_b_2", "Штрафы",    "Штраф за возврат", -500.0, "2025-09-11", "SKU-B"],
    ]
    df = pd.DataFrame(rows, columns=[
        "ID начисления", "Группа услуг", "Тип начисления",
        "Сумма итого, руб", "Дата начисления", "SKU",
    ])
    path = tmp_path / "ozon_sample.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return str(path)


@pytest.fixture
def ozon_pokatushki_csv(tmp_path):
    """3 обратных логистики без продаж → должна сработать Боль 1."""
    rows = []
    for i in range(3):
        rows.append([
            f"acc_fake_{i}",
            "Логистика",
            "Обратная логистика",
            -100.0,
            "2025-09-12",
            "SKU-FAKE",
        ])
    df = pd.DataFrame(rows, columns=[
        "ID начисления", "Группа услуг", "Тип начисления",
        "Сумма итого, руб", "Дата начисления", "SKU",
    ])
    path = tmp_path / "ozon_pokatushki.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return str(path)


@pytest.fixture
def ozon_empty_csv(tmp_path):
    """Файл без строк (только заголовки)."""
    df = pd.DataFrame(columns=[
        "ID начисления", "Группа услуг", "Тип начисления",
        "Сумма итого, руб", "Дата начисления", "SKU",
    ])
    path = tmp_path / "ozon_empty.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return str(path)


@pytest.fixture
def ozon_high_acq_csv(tmp_path):
    """SKU с эквайрингом 5% → должна сработать Боль 2."""
    rows = [
        ["acc_1", "Продажи",   "Продажа",   1000.0, "2025-09-10", "SKU-X"],
        ["acc_2", "Эквайринг", "Эквайринг",   -50.0, "2025-09-10", "SKU-X"],
    ]
    df = pd.DataFrame(rows, columns=[
        "ID начисления", "Группа услуг", "Тип начисления",
        "Сумма итого, руб", "Дата начисления", "SKU",
    ])
    path = tmp_path / "ozon_high_acq.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return str(path)


# ─── Тесты categorize ────────────────────────────────────────────────────────

class TestCategorize:
    def test_sale(self):
        assert categorize("Продажи", "Продажа") == "доход"

    def test_acquiring(self):
        assert categorize("Эквайринг", "Эквайринг") == "эквайринг"

    def test_logistics_drop_off(self):
        assert (
            categorize("Логистика", "Обработка отправления Drop-off (СЦ)")
            == "логистика"
        )

    def test_storage(self):
        assert categorize("Хранение", "Хранение на складе") == "хранение"

    def test_penalty(self):
        assert categorize("Штрафы", "Штраф за недопоставку") == "штрафы"

    def test_partner_promo(self):
        assert (
            categorize("Услуги партнёров", "Продвижение в поиске")
            == "партнёры"
        )

    def test_other(self):
        assert categorize("Что-то ещё", "Непонятная услуга") == "прочее"


# ─── Тесты базового analyze ──────────────────────────────────────────────────

class TestAnalyze:
    def test_analyze_returns_string(self, ozon_sample_csv):
        result = analyze(ozon_sample_csv)
        assert isinstance(result, str)
        assert len(result) > 100

    def test_analyze_contains_ozon_header(self, ozon_sample_csv):
        result = analyze(ozon_sample_csv)
        assert "ОТЧЁТ OZON" in result

    def test_total_profit_correct(self, ozon_sample_csv):
        """SKU-A: 1000 - 15 - 50 = 935 (прибыль)
        SKU-B: 200 - 500 = -300 (убыток)
        Общая: 935 - 300 = 635
        """
        result = analyze(ozon_sample_csv)
        assert "635.00" in result, f"Не нашли '635.00' в:\n{result}"

    def test_sku_a_in_top(self, ozon_sample_csv):
        result = analyze(ozon_sample_csv)
        assert "SKU-A" in result

    def test_sku_b_in_losers(self, ozon_sample_csv):
        result = analyze(ozon_sample_csv)
        assert "SKU-B" in result

    def test_emoji_present(self, ozon_sample_csv):
        result = analyze(ozon_sample_csv)
        for emoji in ("💰", "🚚", "🔴", "✅", "📊"):
            assert emoji in result, f"Нет эмодзи {emoji}"

    def test_html_bold_for_negatives(self, ozon_sample_csv):
        result = analyze(ozon_sample_csv)
        assert "<b>" in result


# ─── Тесты болей ─────────────────────────────────────────────────────────────

class TestPains:
    def test_pokatushki_detected(self, ozon_pokatushki_csv):
        """3 обратных логистики без продаж → Боль 1."""
        result = analyze(ozon_pokatushki_csv)
        assert "ОБНАРУЖЕНЫ АНОМАЛИИ" in result
        assert "обратной логистикой" in result

    def test_pokatushki_threshold_not_triggered_with_one(self, tmp_path):
        """1 обратная логистика → НЕ должно сработать."""
        rows = [
            ["acc_fake_1", "Логистика", "Обратная логистика", -100.0, "2025-09-12", "SKU-FAKE"],
        ]
        df = pd.DataFrame(rows, columns=[
            "ID начисления", "Группа услуг", "Тип начисления",
            "Сумма итого, руб", "Дата начисления", "SKU",
        ])
        path = tmp_path / "single_fake.csv"
        df.to_csv(path, index=False, encoding="utf-8-sig")
        result = analyze(str(path))
        assert "ОБНАРУЖЕНЫ АНОМАЛИИ" not in result

    def test_high_acquiring_detected(self, ozon_high_acq_csv):
        """Эквайринг 5% (норма 1.2-1.7%) → упоминание в отчёте."""
        result = analyze(ozon_high_acq_csv)
        assert "Эквайринг" in result or "эквайринг" in result


# ─── Тесты формата ───────────────────────────────────────────────────────────

class TestFormat:
    def test_wrong_format_raises(self, tmp_path):
        """Файл без Ozon-колонок → ValueError."""
        bad = tmp_path / "wrong.csv"
        bad.write_text("foo,bar\n1,2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="(?i)ozon"):
            analyze(str(bad))

    def test_empty_file_does_not_crash(self, ozon_empty_csv):
        """Пустой отчёт (только заголовки) — должен отработать без crash."""
        result = analyze(ozon_empty_csv)
        assert isinstance(result, str)
        assert "ОТЧЁТ OZON" in result
        assert "0.00" in result


# ─── Интеграция с parser_dispatcher ──────────────────────────────────────────

class TestDispatcher:
    def test_dispatcher_recognizes_ozon_csv(self, ozon_sample_csv):
        from parser_dispatcher import detect_marketplace
        assert detect_marketplace(ozon_sample_csv) == "Ozon"

    def test_dispatcher_routes_ozon_correctly(self, ozon_sample_csv):
        """analyze через dispatcher должен вернуть Ozon-отчёт."""
        from parser_dispatcher import analyze as dispatcher_analyze
        result = dispatcher_analyze(ozon_sample_csv)
        assert "ОТЧЁТ OZON" in result
        assert "635.00" in result

