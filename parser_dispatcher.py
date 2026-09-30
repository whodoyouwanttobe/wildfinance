"""
parser_dispatcher.py — Единая точка входа для анализа отчётов маркетплейсов.

Идея: пользователь присылает файл, не думая WB это или Ozon.
Мы сами определяем формат по заголовкам и вызываем нужный парсер.

Возвращает кортеж: (html_report: str, marketplace: str)
где marketplace ∈ {"WB", "Ozon"} — для статистики.
"""

import os
import pandas as pd


# Сигнатуры форматов (по уникальным заголовкам).
# Каждый ключ — набор столбцов, которые однозначно идентифицируют формат.
WB_SIGNATURE_COLUMNS = (
    "К перечислению за товар",
    "Стоимость логистики",
    "Общая сумма штрафов",
)
WB_SIGNATURE_ALT = (
    "К перечислению Продавцу за реализованный товар",
    "Услуги по доставке товара покупателю",
)

OZON_REQUIRED = ("ID начисления", "Группа услуг", "Тип начисления", "Сумма итого, руб")


def _peek_columns(filepath: str, max_rows: int = 5) -> list[str]:
    """Читает первые строки файла, чтобы посмотреть на заголовки."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".csv":
        for enc in ("utf-8-sig", "cp1251", "utf-8"):
            try:
                df = pd.read_csv(filepath, nrows=max_rows, encoding=enc)
                return list(df.columns)
            except UnicodeDecodeError:
                continue
        return []
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(filepath, nrows=max_rows)
        return list(df.columns)
    return []


def detect_marketplace(filepath: str) -> str:
    """Возвращает 'WB' или 'Ozon' по заголовкам файла."""
    cols = _peek_columns(filepath)
    cols_set = set(cols)

    # WB — формат A
    if any(c in cols_set for c in WB_SIGNATURE_COLUMNS):
        return "WB"
    # WB — формат B
    if any(c in cols_set for c in WB_SIGNATURE_ALT):
        return "WB"

    # Ozon — должны быть все обязательные
    if all(c in cols_set for c in OZON_REQUIRED):
        return "Ozon"

    raise ValueError(
        "Не удалось определить маркетплейс по заголовкам файла.\n"
        f"Найдены столбцы: {', '.join(cols[:8])}...\n\n"
        "Поддерживаются:\n"
        "  • WB: детализированный отчёт по товарам (.xlsx/.csv)\n"
        "  • Ozon: детализация начислений из seller.ozon.ru/app/finances/accruals"
    )


def analyze(filepath: str) -> str:
    """
    Единая точка входа. Возвращает HTML-отчёт.
    Сама определяет маркетплейс и вызывает нужный парсер.
    """
    marketplace = detect_marketplace(filepath)
    if marketplace == "WB":
        from wb_parser import analyze as wb_analyze
        return wb_analyze(filepath)
    elif marketplace == "Ozon":
        from ozon_parser import analyze as ozon_analyze
        return ozon_analyze(filepath)
    # Теоретически недостижимо (detect_marketplace всегда бросает или возвращает)
    raise ValueError(f"Неизвестный маркетплейс: {marketplace}")


def compute(filepath: str) -> dict:
    """
    Метрики отчёта без форматирования (для /compare).
    dict с ключами: marketplace, доход, чистая_прибыль, ..., grouped (DataFrame по SKU).
    """
    marketplace = detect_marketplace(filepath)
    if marketplace == "WB":
        from wb_parser import compute as wb_compute
        return wb_compute(filepath)
    from ozon_parser import compute as ozon_compute
    return ozon_compute(filepath)
