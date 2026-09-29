"""
generate_wb_report.py — Генератор реалистичного детализированного отчёта Wildberries.

Создаёт файл wb_real_report.xlsx с ~500 строками операций за неделю,
включающий все столбцы из документации WB.
"""

import random
import string
import uuid
from datetime import datetime, timedelta
import pandas as pd

random.seed(42)

# ═══════════════════════════════════════════════════════════════════
# Справочники товаров (15 артикулов, разные категории)
# ═══════════════════════════════════════════════════════════════════

PRODUCTS = [
    {
        "predmet": "Футболки", "brand": "UrbanWear",
        "artikul": "UW-TSHIRT-001", "name": "Футболка мужская хлопок оверсайз",
        "nom_code": 198745632, "barcode": "2037489561234",
        "sizes": ["S", "M", "L", "XL", "XXL"],
        "retail_price": 1890, "kvv_pct": 15.0,
    },
    {
        "predmet": "Футболки", "brand": "UrbanWear",
        "artikul": "UW-TSHIRT-002", "name": "Футболка женская приталенная",
        "nom_code": 198745633, "barcode": "2037489561235",
        "sizes": ["XS", "S", "M", "L"],
        "retail_price": 1690, "kvv_pct": 15.0,
    },
    {
        "predmet": "Джинсы", "brand": "DenimPro",
        "artikul": "DP-JEANS-MOM-01", "name": "Джинсы женские mom fit голубые",
        "nom_code": 276583410, "barcode": "2098374651001",
        "sizes": ["25", "26", "27", "28", "29", "30"],
        "retail_price": 3490, "kvv_pct": 14.0,
    },
    {
        "predmet": "Джинсы", "brand": "DenimPro",
        "artikul": "DP-JEANS-SLIM-02", "name": "Джинсы мужские slim тёмно-синие",
        "nom_code": 276583411, "barcode": "2098374651002",
        "sizes": ["30", "31", "32", "33", "34"],
        "retail_price": 3790, "kvv_pct": 14.0,
    },
    {
        "predmet": "Кроссовки", "brand": "StepMax",
        "artikul": "SM-SNKR-RUN-44", "name": "Кроссовки беговые мужские StepMax Air",
        "nom_code": 312094875, "barcode": "2145098763210",
        "sizes": ["41", "42", "43", "44", "45"],
        "retail_price": 5990, "kvv_pct": 13.5,
    },
    {
        "predmet": "Кроссовки", "brand": "StepMax",
        "artikul": "SM-SNKR-CSL-38", "name": "Кроссовки повседневные женские StepMax Lite",
        "nom_code": 312094876, "barcode": "2145098763211",
        "sizes": ["36", "37", "38", "39", "40"],
        "retail_price": 4990, "kvv_pct": 13.5,
    },
    {
        "predmet": "Худи", "brand": "UrbanWear",
        "artikul": "UW-HOODIE-BLK", "name": "Худи унисекс оверсайз чёрное",
        "nom_code": 198745700, "barcode": "2037489561300",
        "sizes": ["S", "M", "L", "XL"],
        "retail_price": 2990, "kvv_pct": 15.0,
    },
    {
        "predmet": "Носки", "brand": "SockLab",
        "artikul": "SL-SOCK-PACK-5", "name": "Носки мужские набор 5 пар хлопок",
        "nom_code": 410293847, "barcode": "2200198374650",
        "sizes": ["25-27", "27-29", "29-31"],
        "retail_price": 690, "kvv_pct": 17.0,
    },
    {
        "predmet": "Рюкзаки", "brand": "CarryOn",
        "artikul": "CO-BKPK-URBAN", "name": "Рюкзак городской с USB портом",
        "nom_code": 523098174, "barcode": "2301847562009",
        "sizes": ["0"],
        "retail_price": 2490, "kvv_pct": 14.5,
    },
    {
        "predmet": "Сумки", "brand": "CarryOn",
        "artikul": "CO-BAG-TOTE-01", "name": "Сумка-тоут женская эко-кожа",
        "nom_code": 523098175, "barcode": "2301847562010",
        "sizes": ["0"],
        "retail_price": 1990, "kvv_pct": 14.5,
    },
    {
        "predmet": "Платья", "brand": "LaDonna",
        "artikul": "LD-DRESS-MIDI", "name": "Платье миди с цветочным принтом",
        "nom_code": 645098231, "barcode": "2409182736450",
        "sizes": ["XS", "S", "M", "L"],
        "retail_price": 3290, "kvv_pct": 15.0,
    },
    {
        "predmet": "Куртки", "brand": "NordWind",
        "artikul": "NW-JACKET-LITE", "name": "Куртка демисезонная мужская лёгкая",
        "nom_code": 734019285, "barcode": "2510293847561",
        "sizes": ["M", "L", "XL", "XXL"],
        "retail_price": 6490, "kvv_pct": 13.0,
    },
    {
        "predmet": "Шорты", "brand": "UrbanWear",
        "artikul": "UW-SHORTS-CARGO", "name": "Шорты мужские карго хлопок",
        "nom_code": 198745750, "barcode": "2037489561350",
        "sizes": ["M", "L", "XL"],
        "retail_price": 1990, "kvv_pct": 15.0,
    },
    {
        "predmet": "Белье", "brand": "ComfortZone",
        "artikul": "CZ-BRA-SPORT-01", "name": "Бюстгальтер спортивный бесшовный",
        "nom_code": 867293041, "barcode": "2618293740156",
        "sizes": ["S", "M", "L"],
        "retail_price": 890, "kvv_pct": 16.0,
    },
    {
        "predmet": "Аксессуары", "brand": "StyleUp",
        "artikul": "SU-BELT-LEATHER", "name": "Ремень кожаный классический",
        "nom_code": 945018273, "barcode": "2719384650102",
        "sizes": ["90", "100", "110", "120"],
        "retail_price": 1290, "kvv_pct": 14.0,
    },
]

WAREHOUSES = [
    "Коледино", "Подольск", "Электросталь", "Казань",
    "Краснодар", "Тула", "Белая Дача", "Хоругвино",
]

PVZ_OFFICES = [
    ("Москва, ул. Ленина, 15", "MSK-001"),
    ("Санкт-Петербург, Невский пр., 42", "SPB-012"),
    ("Казань, ул. Баумана, 8", "KZN-003"),
    ("Екатеринбург, ул. Малышева, 33", "EKB-007"),
    ("Новосибирск, ул. Красный пр., 65", "NSK-005"),
    ("Краснодар, ул. Красная, 109", "KRD-009"),
    ("Ростов-на-Дону, пр. Будённовский, 21", "RND-004"),
    ("Нижний Новгород, ул. Большая Покровская, 18", "NNG-006"),
    ("Воронеж, ул. Плехановская, 22", "VRN-008"),
    ("Самара, ул. Ленинградская, 56", "SMR-010"),
]

BANKS = ["Сбербанк", "Тинькофф Банк", "Альфа-Банк", "ВТБ"]

PENALTY_TYPES = [
    "Штраф за недопоставку товара",
    "Штраф за несвоевременную поставку",
    "Штраф за отсутствие маркировки",
    "Штраф за нарушение упаковки",
    "Штраф за подмену товара",
]

DEDUCTION_TYPES = [
    "WB Продвижение — автокампания",
    "WB Продвижение — поиск",
    "Минимальный платёж Конструктор тарифов",
    "Подписка Джем",
]

REPORT_START = datetime(2025, 9, 15)
REPORT_END = datetime(2025, 9, 21)


def rand_date(start=REPORT_START, end=REPORT_END):
    delta = (end - start).total_seconds()
    return start + timedelta(seconds=random.randint(0, int(delta)))


def rand_srid():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=32))


def rand_shk():
    return "".join(random.choices(string.digits, k=13))


def rand_sticker():
    return f"MP-{''.join(random.choices(string.digits, k=10))}"


def fmt_date(dt):
    return dt.strftime("%Y-%m-%d")


def make_empty_row():
    """Шаблон пустой строки со всеми столбцами."""
    return {
        "№": 0,
        "Номер поставки": 0,
        "Предмет": "",
        "Код номенклатуры": 0,
        "Бренд": "",
        "Артикул поставщика": "",
        "Название": "",
        "Размер": "0",
        "Баркод": "",
        "Тип документа": "",
        "Обоснование для оплаты": "",
        "Дата заказа покупателем": "",
        "Дата продажи": "",
        "Кол-во": 0,
        "Цена розничная": 0,
        "Вайлдберриз реализовал товар (Пр)": 0,
        "Согласованный продуктовый дисконт, %": 0,
        "Промокод, %": 0,
        "Итоговая согласованная скидка, %": 0,
        "Цена розничная с учётом согласованной скидки": 0,
        "Размер изменения кВВ из-за рейтинга, %": 0,
        "Размер изменения кВВ из-за акции, %": 0,
        "Платформенные скидки, %": 0,
        "Размер кВВ, %": 0,
        "Размер кВВ без НДС, % Базовый": 0,
        "Итоговый кВВ без НДС, %": 0,
        "Вознаграждение с продаж до вычета услуг поверенного, без НДС": 0,
        "Возмещение за выдачу и возврат товаров на ПВЗ": 0,
        "Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов": 0,
        "Размер компенсации платёжных услуг/комиссии за интеграцию платёжных сервисов, %": 0,
        "Тип платежа: компенсация платёжных услуг/комиссия за интеграцию платёжных сервисов": "",
        "Вознаграждение Вайлдберриз (ВВ), без НДС": 0,
        "НДС с Вознаграждения Вайлдберриз": 0,
        "К перечислению Продавцу за реализованный товар": 0,
        "Количество доставок": 0,
        "Количество возврата": 0,
        "Услуги по доставке товара покупателю": 0,
        "Дата начала действия фиксации": "",
        "Дата конца действия фиксации": "",
        "Признак услуги платной доставки": "",
        "Общая сумма штрафов": 0,
        "Корректировка Вознаграждения Вайлдберриз (ВВ)": 0,
        "Виды доставок, штрафов и корректировок ВВ": "",
        "Стикер МП": "",
        "Наименование банка-эквайера": "",
        "Номер офиса": "",
        "Наименование офиса доставки": "",
        "ИНН партнёра": "",
        "Партнёр": "",
        "Склад": "",
        "Страна": "",
        "Тип коробов": "",
        "Номер таможенной декларации": "",
        "Номер сборочного задания": 0,
        "Код маркировки": "",
        "ШК": "",
        "Srid": "",
        "Возмещение издержек по перевозке/по складским операциям с товаром": 0,
        "Организатор перевозки": "",
        "Хранение": 0,
        "Удержания": 0,
        "Операции при приёмке": 0,
        "Фиксированный коэффициент склада по поставке": 0,
        "Признак продажи юридическому лицу": "",
        "Номер короба для обработки товара": "",
        "Скидка по программе софинансирования": "",
        "Скидка Wibes, %": 0,
        "Компенсация скидки по программе лояльности": 0,
        "Стоимость участия в программе лояльности": 0,
        "Сумма, удержанная за начисленные баллы программы лояльности": 0,
        "ID корзины заказа": 0,
        "Разовое изменение срока перечисления денежных средств": 0,
        "ID собственной акции продавца с дополнительной скидкой": 0,
        "Размер дополнительной скидки по собственной акции продавца, %": 0,
        "Способ продажи и тип товара": "",
        "Уникальный идентификатор скидки лояльности от продавца": 0,
        "Размер скидки лояльности от продавца, %": 0,
        "ID промокода": 0,
        "Скидка за промокод": 0,
        "ID подменного артикула": 0,
        "Скидка по подменному артикулу, %": 0,
        "Оптовая скидка для бизнеса, %": 0,
        "ИНН покупателя-юрлица или ИП": "",
        "Оплата социальным сертификатом": "",
    }


def generate_sale_row(product, order_date, sale_date, srid, supply_num, warehouse):
    """Генерирует строку продажи."""
    row = make_empty_row()
    size = random.choice(product["sizes"])
    retail = product["retail_price"]
    platform_discount = round(random.uniform(2, 12), 1)
    wb_sold = round(retail * (1 - platform_discount / 100), 2)
    kvv = product["kvv_pct"]
    kvv_no_vat_base = round(kvv / 1.2, 2)
    kvv_no_vat_final = round(kvv_no_vat_base - platform_discount, 2)
    if kvv_no_vat_final < 0:
        kvv_no_vat_final = 0.5
    reward_before = round(wb_sold * kvv_no_vat_final / 100, 2)
    pvz_comp = round(random.uniform(1, 8), 2)
    payment_pct = round(random.uniform(1.5, 2.5), 2)
    payment_comp = round(wb_sold * payment_pct / 100, 2)
    vv_no_vat = round(reward_before + pvz_comp + payment_comp, 2)
    vat_vv = round(vv_no_vat * 0.2, 2)
    to_seller = round(wb_sold - vv_no_vat - vat_vv, 2)
    bank = random.choice(BANKS)
    pvz = random.choice(PVZ_OFFICES)
    basket_id = random.randint(100000000, 999999999)

    row.update({
        "Номер поставки": supply_num,
        "Предмет": product["predmet"],
        "Код номенклатуры": product["nom_code"],
        "Бренд": product["brand"],
        "Артикул поставщика": product["artikul"],
        "Название": product["name"],
        "Размер": size,
        "Баркод": product["barcode"],
        "Тип документа": "Продажа",
        "Обоснование для оплаты": "Продажа",
        "Дата заказа покупателем": fmt_date(order_date),
        "Дата продажи": fmt_date(sale_date),
        "Кол-во": 1,
        "Цена розничная": retail,
        "Вайлдберриз реализовал товар (Пр)": wb_sold,
        "Цена розничная с учётом согласованной скидки": retail,
        "Платформенные скидки, %": platform_discount,
        "Размер кВВ, %": kvv,
        "Размер кВВ без НДС, % Базовый": kvv_no_vat_base,
        "Итоговый кВВ без НДС, %": kvv_no_vat_final,
        "Вознаграждение с продаж до вычета услуг поверенного, без НДС": reward_before,
        "Возмещение за выдачу и возврат товаров на ПВЗ": pvz_comp,
        "Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов": payment_comp,
        "Размер компенсации платёжных услуг/комиссии за интеграцию платёжных сервисов, %": payment_pct,
        "Тип платежа: компенсация платёжных услуг/комиссия за интеграцию платёжных сервисов": "компенсация платёжных услуг",
        "Вознаграждение Вайлдберриз (ВВ), без НДС": vv_no_vat,
        "НДС с Вознаграждения Вайлдберриз": vat_vv,
        "К перечислению Продавцу за реализованный товар": to_seller,
        "Наименование банка-эквайера": bank,
        "Номер офиса": pvz[1],
        "Наименование офиса доставки": pvz[0],
        "Склад": warehouse,
        "Страна": "Россия",
        "Тип коробов": random.choice(["Моно", "Микс"]),
        "ШК": rand_shk(),
        "Srid": srid,
        "ID корзины заказа": basket_id,
    })
    return row


def generate_return_row(product, sale_date, srid, supply_num, warehouse):
    """Генерирует строку возврата (зеркало продажи, отрицательная сумма)."""
    row = make_empty_row()
    size = random.choice(product["sizes"])
    retail = product["retail_price"]
    platform_discount = round(random.uniform(2, 12), 1)
    wb_sold = round(retail * (1 - platform_discount / 100), 2)
    kvv = product["kvv_pct"]
    kvv_no_vat_base = round(kvv / 1.2, 2)
    kvv_no_vat_final = round(kvv_no_vat_base - platform_discount, 2)
    if kvv_no_vat_final < 0:
        kvv_no_vat_final = 0.5
    reward_before = round(wb_sold * kvv_no_vat_final / 100, 2)
    pvz_comp = round(random.uniform(1, 8), 2)
    payment_pct = round(random.uniform(1.5, 2.5), 2)
    payment_comp = round(wb_sold * payment_pct / 100, 2)
    vv_no_vat = round(reward_before + pvz_comp + payment_comp, 2)
    vat_vv = round(vv_no_vat * 0.2, 2)
    to_seller = round(wb_sold - vv_no_vat - vat_vv, 2)
    pvz = random.choice(PVZ_OFFICES)

    row.update({
        "Номер поставки": supply_num,
        "Предмет": product["predmet"],
        "Код номенклатуры": product["nom_code"],
        "Бренд": product["brand"],
        "Артикул поставщика": product["artikul"],
        "Название": product["name"],
        "Размер": size,
        "Баркод": product["barcode"],
        "Тип документа": "Возврат",
        "Обоснование для оплаты": "Возврат",
        "Дата заказа покупателем": fmt_date(sale_date - timedelta(days=random.randint(3, 10))),
        "Дата продажи": fmt_date(sale_date),
        "Кол-во": 1,
        "Цена розничная": retail,
        "Вайлдберриз реализовал товар (Пр)": wb_sold,
        "Цена розничная с учётом согласованной скидки": retail,
        "Платформенные скидки, %": platform_discount,
        "Размер кВВ, %": kvv,
        "Размер кВВ без НДС, % Базовый": kvv_no_vat_base,
        "Итоговый кВВ без НДС, %": kvv_no_vat_final,
        "Вознаграждение с продаж до вычета услуг поверенного, без НДС": -reward_before,
        "Возмещение за выдачу и возврат товаров на ПВЗ": -pvz_comp,
        "Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов": -payment_comp,
        "Размер компенсации платёжных услуг/комиссии за интеграцию платёжных сервисов, %": payment_pct,
        "Тип платежа: компенсация платёжных услуг/комиссия за интеграцию платёжных сервисов": "компенсация платёжных услуг",
        "Вознаграждение Вайлдберриз (ВВ), без НДС": -vv_no_vat,
        "НДС с Вознаграждения Вайлдберриз": -vat_vv,
        "К перечислению Продавцу за реализованный товар": -to_seller,
        "Номер офиса": pvz[1],
        "Наименование офиса доставки": pvz[0],
        "Склад": warehouse,
        "Страна": "Россия",
        "ШК": rand_shk(),
        "Srid": srid,
    })
    return row


def generate_logistics_row(product, date, srid, warehouse, delivery_type, is_return=False):
    """Генерирует строку логистики."""
    row = make_empty_row()

    # Стоимость доставки зависит от категории
    base_cost = random.uniform(40, 120)
    if product["predmet"] in ("Кроссовки", "Куртки", "Рюкзаки"):
        base_cost = random.uniform(80, 200)
    delivery_cost = round(base_cost, 2)

    fix_coeff = round(random.uniform(0.8, 1.5), 2)
    fix_start = fmt_date(REPORT_START - timedelta(days=14))
    fix_end = fmt_date(REPORT_END + timedelta(days=14))

    sales_method = random.choice([
        "FBW, (МГТ, короба)", "FBW, (МГТ, паллеты)", "FBS, (МГТ)",
    ])

    row.update({
        "Предмет": product["predmet"],
        "Код номенклатуры": product["nom_code"],
        "Бренд": product["brand"],
        "Артикул поставщика": product["artikul"],
        "Название": product["name"],
        "Размер": random.choice(product["sizes"]),
        "Баркод": product["barcode"],
        "Обоснование для оплаты": "Логистика",
        "Дата продажи": fmt_date(date),
        "Количество доставок": 0 if is_return else 1,
        "Количество возврата": 1 if is_return else 0,
        "Услуги по доставке товара покупателю": delivery_cost,
        "Дата начала действия фиксации": fix_start,
        "Дата конца действия фиксации": fix_end,
        "Виды доставок, штрафов и корректировок ВВ": delivery_type,
        "Склад": warehouse,
        "Страна": "Россия",
        "ШК": rand_shk(),
        "Srid": srid,
        "Фиксированный коэффициент склада по поставке": fix_coeff,
        "Способ продажи и тип товара": sales_method,
    })
    return row


def generate_storage_row(date, amount):
    """Генерирует строку хранения (без привязки к товару)."""
    row = make_empty_row()
    row.update({
        "Обоснование для оплаты": "Хранение",
        "Дата продажи": fmt_date(date),
        "Виды доставок, штрафов и корректировок ВВ": "Хранение",
        "Хранение": round(amount, 2),
        "Srid": rand_srid(),
    })
    return row


def generate_penalty_row(product, date, penalty_type, amount):
    """Генерирует строку штрафа."""
    row = make_empty_row()
    row.update({
        "Предмет": product["predmet"],
        "Код номенклатуры": product["nom_code"],
        "Бренд": product["brand"],
        "Артикул поставщика": product["artikul"],
        "Название": product["name"],
        "Баркод": product["barcode"],
        "Обоснование для оплаты": "Штраф",
        "Дата продажи": fmt_date(date),
        "Общая сумма штрафов": round(amount, 2),
        "Виды доставок, штрафов и корректировок ВВ": penalty_type,
        "Склад": random.choice(WAREHOUSES),
        "Srid": rand_srid(),
    })
    return row


def generate_deduction_row(date, deduction_type, amount):
    """Генерирует строку удержания."""
    row = make_empty_row()
    row.update({
        "Обоснование для оплаты": "Удержания",
        "Дата продажи": fmt_date(date),
        "Виды доставок, штрафов и корректировок ВВ": deduction_type,
        "Удержания": round(amount, 2),
        "Srid": rand_srid(),
    })
    return row


def generate_acceptance_row(date, supply_num, amount):
    """Генерирует строку приёмки."""
    row = make_empty_row()
    row.update({
        "Номер поставки": supply_num,
        "Обоснование для оплаты": "Обработка товара",
        "Дата продажи": fmt_date(date),
        "Виды доставок, штрафов и корректировок ВВ": "Обработка товара",
        "Операции при приёмке": round(amount, 2),
        "Склад": random.choice(WAREHOUSES),
        "Srid": rand_srid(),
    })
    return row


def generate_compensation_row(product, date, srid, warehouse):
    """Генерирует строку компенсации ущерба."""
    row = make_empty_row()
    comp_amount = round(product["retail_price"] * random.uniform(0.6, 0.9), 2)

    row.update({
        "Предмет": product["predmet"],
        "Код номенклатуры": product["nom_code"],
        "Бренд": product["brand"],
        "Артикул поставщика": product["artikul"],
        "Название": product["name"],
        "Размер": random.choice(product["sizes"]),
        "Баркод": product["barcode"],
        "Тип документа": "Продажа",
        "Обоснование для оплаты": "Компенсация ущерба",
        "Дата продажи": fmt_date(date),
        "Кол-во": 1,
        "К перечислению Продавцу за реализованный товар": comp_amount,
        "Склад": warehouse,
        "Страна": "Россия",
        "ШК": rand_shk(),
        "Srid": srid,
    })
    return row


def main():
    rows = []
    supply_numbers = [random.randint(10000000, 99999999) for _ in range(5)]

    # ════════════════════════════════════════════════
    # 1. ПРОДАЖИ + ЛОГИСТИКА (основная масса строк)
    # ════════════════════════════════════════════════
    # ~300 продаж, каждая с доставкой
    for _ in range(320):
        product = random.choice(PRODUCTS)
        warehouse = random.choice(WAREHOUSES)
        supply = random.choice(supply_numbers)
        order_date = rand_date(REPORT_START - timedelta(days=5), REPORT_START + timedelta(days=3))
        sale_date = rand_date(
            max(order_date, REPORT_START),
            min(order_date + timedelta(days=5), REPORT_END),
        )
        srid = rand_srid()

        # Строка продажи
        rows.append(generate_sale_row(product, order_date, sale_date, srid, supply, warehouse))

        # Строка логистики (доставка к клиенту)
        logistics_date = sale_date - timedelta(days=random.randint(0, 2))
        rows.append(generate_logistics_row(
            product, logistics_date, srid, warehouse,
            "к клиенту при продаже", is_return=False
        ))

    # ════════════════════════════════════════════════
    # 2. ВОЗВРАТЫ + ОБРАТНАЯ ЛОГИСТИКА (~15% от продаж)
    # ════════════════════════════════════════════════
    for _ in range(48):
        product = random.choice(PRODUCTS)
        warehouse = random.choice(WAREHOUSES)
        supply = random.choice(supply_numbers)
        return_date = rand_date()
        srid = rand_srid()

        rows.append(generate_return_row(product, return_date, srid, supply, warehouse))

        # Обратная логистика
        rows.append(generate_logistics_row(
            product, return_date, srid, warehouse,
            "от клиента при возврате", is_return=True
        ))

    # ════════════════════════════════════════════════
    # 3. ОТКАЗЫ + ЛОГИСТИКА (доставка и возврат без выкупа)
    # ════════════════════════════════════════════════
    for _ in range(25):
        product = random.choice(PRODUCTS)
        warehouse = random.choice(WAREHOUSES)
        srid = rand_srid()
        date = rand_date()

        # Логистика к клиенту при отмене
        rows.append(generate_logistics_row(
            product, date, srid, warehouse,
            "к клиенту при отмене", is_return=False
        ))
        # Обратная от клиента при отмене
        rows.append(generate_logistics_row(
            product, date + timedelta(days=1), srid, warehouse,
            "от клиента при отмене", is_return=True
        ))

    # ════════════════════════════════════════════════
    # 4. ХРАНЕНИЕ (1 запись за каждый день недели)
    # ════════════════════════════════════════════════
    for day_offset in range(7):
        date = REPORT_START + timedelta(days=day_offset)
        daily_storage = round(random.uniform(800, 2500), 2)
        rows.append(generate_storage_row(date, daily_storage))

    # ════════════════════════════════════════════════
    # 5. ШТРАФЫ (~8 штук за неделю)
    # ════════════════════════════════════════════════
    for _ in range(8):
        product = random.choice(PRODUCTS)
        date = rand_date()
        penalty_type = random.choice(PENALTY_TYPES)
        amount = round(random.uniform(100, 3000), 2)
        rows.append(generate_penalty_row(product, date, penalty_type, amount))

    # ════════════════════════════════════════════════
    # 6. УДЕРЖАНИЯ (рекламные кампании, подписки)
    # ════════════════════════════════════════════════
    for _ in range(12):
        date = rand_date()
        deduction_type = random.choice(DEDUCTION_TYPES)
        amount = round(random.uniform(200, 5000), 2)
        rows.append(generate_deduction_row(date, deduction_type, amount))

    # ════════════════════════════════════════════════
    # 7. ПРИЁМКА (2-3 поставки за неделю)
    # ════════════════════════════════════════════════
    for supply in supply_numbers[:3]:
        date = rand_date()
        amount = round(random.uniform(500, 3000), 2)
        rows.append(generate_acceptance_row(date, supply, amount))

    # ════════════════════════════════════════════════
    # 8. КОМПЕНСАЦИИ УЩЕРБА (2-3 штуки)
    # ════════════════════════════════════════════════
    for _ in range(3):
        product = random.choice(PRODUCTS)
        warehouse = random.choice(WAREHOUSES)
        date = rand_date()
        srid = rand_srid()
        rows.append(generate_compensation_row(product, date, srid, warehouse))

    # ════════════════════════════════════════════════
    # Нумерация строк и сортировка
    # ════════════════════════════════════════════════
    # Сортируем по дате продажи
    rows.sort(key=lambda r: r.get("Дата продажи", "9999"))

    for i, row in enumerate(rows, 1):
        row["№"] = i

    df = pd.DataFrame(rows)

    # Сохраняем как Excel
    output_xlsx = "wb_real_report.xlsx"
    df.to_excel(output_xlsx, index=False, engine="openpyxl")

    # Также сохраняем как CSV
    output_csv = "wb_real_report.csv"
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")

    print(f"Создано строк: {len(rows)}")
    print(f"Файлы сохранены:")
    print(f"  Excel: {output_xlsx}")
    print(f"  CSV:   {output_csv}")
    print()

    # Быстрая статистика
    sales = sum(1 for r in rows if r["Обоснование для оплаты"] == "Продажа")
    returns = sum(1 for r in rows if r["Обоснование для оплаты"] == "Возврат")
    logistics = sum(1 for r in rows if r["Обоснование для оплаты"] == "Логистика")
    storage = sum(1 for r in rows if r["Обоснование для оплаты"] == "Хранение")
    penalties = sum(1 for r in rows if r["Обоснование для оплаты"] == "Штраф")
    deductions = sum(1 for r in rows if r["Обоснование для оплаты"] == "Удержания")
    acceptance = sum(1 for r in rows if r["Обоснование для оплаты"] == "Обработка товара")
    compensations = sum(1 for r in rows if r["Обоснование для оплаты"] == "Компенсация ущерба")

    total_income = sum(r["К перечислению Продавцу за реализованный товар"] for r in rows)
    total_logistics = sum(r["Услуги по доставке товара покупателю"] for r in rows)
    total_penalties = sum(r["Общая сумма штрафов"] for r in rows)
    total_storage = sum(r["Хранение"] for r in rows)
    total_deductions = sum(r["Удержания"] for r in rows)
    total_acceptance = sum(r["Операции при приёмке"] for r in rows)

    print("--- Состав отчёта ---")
    print(f"  Продаж:        {sales}")
    print(f"  Возвратов:     {returns}")
    print(f"  Логистика:     {logistics}")
    print(f"  Хранение:      {storage}")
    print(f"  Штрафов:       {penalties}")
    print(f"  Удержаний:     {deductions}")
    print(f"  Приёмка:       {acceptance}")
    print(f"  Компенсаций:   {compensations}")
    print()
    print("--- Финансовые итоги ---")
    print(f"  К перечислению:  {total_income:>12,.2f} руб.")
    print(f"  Логистика:       {total_logistics:>12,.2f} руб.")
    print(f"  Штрафы:          {total_penalties:>12,.2f} руб.")
    print(f"  Хранение:        {total_storage:>12,.2f} руб.")
    print(f"  Удержания:       {total_deductions:>12,.2f} руб.")
    print(f"  Приёмка:         {total_acceptance:>12,.2f} руб.")


if __name__ == "__main__":
    main()
