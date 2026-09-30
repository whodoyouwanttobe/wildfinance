"""
table_reader.py — Надёжное чтение отчётов WB/Ozon из Excel и CSV.

Реальные выгрузки часто отличаются от «идеальных» тестовых таблиц:
  • над таблицей бывают строки-заголовки («Отчёт по начислениям за период …»);
  • CSV Ozon разделён точкой с запятой, а не запятой;
  • в CSV числа записаны строками с пробелами и запятой: «1 234,56»;
  • в Excel может быть несколько листов, таблица — не на первом;
  • в названиях столбцов встречаются лишние пробелы и переносы строк.

read_table() находит строку с заголовками по известным названиям столбцов
и возвращает обычный DataFrame. to_num() превращает любые «числа» в float.
"""

from __future__ import annotations

import io
import os
import re

import pandas as pd

# Столбцы, по которым узнаём строку заголовков (WB + Ozon)
KNOWN_HEADERS = {
    # WB — детализация еженедельного отчёта
    "Артикул поставщика", "Обоснование для оплаты", "Тип документа",
    "К перечислению Продавцу за реализованный товар", "К перечислению за товар",
    "Услуги по доставке товара покупателю", "Стоимость логистики",
    "Общая сумма штрафов", "Хранение", "Удержания", "Операции при приёмке",
    "Код номенклатуры", "Srid",
    # Ozon — отчёт по начислениям
    "ID начисления", "Группа услуг", "Тип начисления", "Сумма итого, руб",
    "Дата начисления", "SKU", "Артикул",
}

MAX_HEADER_SCAN_ROWS = 40
_MIN_MATCHES = 2


def _clean(name) -> str:
    return re.sub(r"\s+", " ", str(name)).strip()


def _find_header_row(raw: pd.DataFrame) -> int | None:
    best, best_hits = None, 0
    for i in range(min(len(raw), MAX_HEADER_SCAN_ROWS)):
        cells = {_clean(v) for v in raw.iloc[i].tolist() if pd.notna(v)}
        hits = len(cells & KNOWN_HEADERS)
        if hits > best_hits:
            best, best_hits = i, hits
    return best if best_hits >= _MIN_MATCHES else None


def _frame_from_raw(raw: pd.DataFrame, header_row: int) -> pd.DataFrame:
    columns = [_clean(c) if pd.notna(c) else f"col_{j}" for j, c in enumerate(raw.iloc[header_row])]
    df = raw.iloc[header_row + 1:].copy()
    df.columns = columns
    df = df.dropna(how="all").reset_index(drop=True)
    # Убираем строку «Итого», если она есть в конце таблицы
    first_col = df.columns[0]
    mask_total = df[first_col].astype(str).str.strip().str.lower().isin({"итого", "всего", "итого:"})
    return df[~mask_total].reset_index(drop=True)


def _read_text(filepath: str) -> str:
    raw = open(filepath, "rb").read()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Не удалось прочитать CSV: неизвестная кодировка.")


def _read_csv(filepath: str) -> pd.DataFrame:
    """CSV: сами находим строку заголовков и разделитель (; , или табуляция)."""
    text = _read_text(filepath)
    lines = text.splitlines()
    best = (0, ",", 0)  # (строка, разделитель, совпадений)
    for i, line in enumerate(lines[:MAX_HEADER_SCAN_ROWS]):
        for sep in (";", ",", "\t"):
            cells = {_clean(c.strip().strip('"')) for c in line.split(sep)}
            hits = len(cells & KNOWN_HEADERS)
            if hits > best[2]:
                best = (i, sep, hits)
    row, sep, hits = best
    if hits < _MIN_MATCHES:
        row, sep = 0, None
    df = pd.read_csv(io.StringIO(text), sep=sep, engine="python", skiprows=row,
                     header=0, dtype=str, skip_blank_lines=True)
    df.columns = [_clean(c) for c in df.columns]
    return _frame_from_raw(pd.concat([pd.DataFrame([df.columns], columns=df.columns), df]), 0)


def read_table(filepath: str) -> pd.DataFrame:
    """Читает отчёт (.xlsx/.xls/.csv) и находит таблицу с заголовками."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".csv":
        return _read_csv(filepath)
    if ext in (".xlsx", ".xls"):
        sheets = pd.read_excel(filepath, sheet_name=None, header=None)
        first = None
        for raw in sheets.values():
            if raw.empty:
                continue
            first = raw if first is None else first
            row = _find_header_row(raw)
            if row is not None:
                return _frame_from_raw(raw, row)
        if first is None:
            raise ValueError("Файл пустой: в нём нет ни одной таблицы.")
        return _frame_from_raw(first, 0)
    raise ValueError(f"Неподдерживаемый формат файла: {ext}. Нужен .csv или .xlsx")


def to_num(series: pd.Series) -> pd.Series:
    """«1 234,56» / «-45,5» / 1234.5 / пусто → float (пусто и мусор → 0)."""
    if pd.api.types.is_numeric_dtype(series):
        return series.fillna(0).astype(float)
    s = (
        series.astype(str)
        .str.replace(" ", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace(",", ".", regex=False)
        .str.replace("₽", "", regex=False)
        .str.replace("руб.", "", regex=False)
    )
    return pd.to_numeric(s, errors="coerce").fillna(0.0)
