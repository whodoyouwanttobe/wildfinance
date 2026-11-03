"""Тесты отзывов с сайта: проверка формы, лимит, модерация."""

import pytest

import reviews


def form(**kw):
    base = {"name": "Иван", "role": "ГИП", "company": "ООО Проект", "solution": "wpm",
            "text": "Печать комплекта теперь занимает десять минут.", "consent": True, "stars": 5}
    return base | kw


def test_validate_ok():
    r = reviews.validate(form(solution="WPM<script>"))
    assert r["name"] == "Иван" and r["solution"] == "wpmscript"


@pytest.mark.parametrize("bad", [form(name=""), form(text="коротко"), form(consent=False), "строка",
                                 form(stars=0), form(stars=6), form(stars="пять"), form(stars=None)])
def test_validate_errors(bad):
    with pytest.raises(ValueError):
        reviews.validate(bad)


def test_honeypot():
    with pytest.raises(ValueError, match="spam"):
        reviews.validate(form(website="http://spam"))


def test_rate_limit():
    reviews._hits.clear()
    assert all(reviews.allowed("1.1.1.1", now=1000 + i) for i in range(reviews.PER_HOUR))
    assert not reviews.allowed("1.1.1.1", now=1010)
    assert reviews.allowed("1.1.1.1", now=1000 + 3700)      # через час снова можно
    assert reviews.allowed("2.2.2.2", now=1010)


def test_moderation(tmp_path):
    p = str(tmp_path / "r.db")
    rid = reviews.add(reviews.validate(form()), "1.1.1.1", db_path=p)
    assert reviews.approved(p) == []                         # до модерации не виден
    assert reviews.set_status(rid, "approved", db_path=p)["status"] == "approved"
    pub = reviews.approved(p)
    assert len(pub) == 1 and pub[0]["name"] == "Иван" and "ip" not in pub[0]
    reviews.set_status(rid, "rejected", db_path=p)
    assert reviews.approved(p) == []
    assert reviews.set_status(999, "approved", db_path=p) is None


def test_anonymous(tmp_path):
    p = str(tmp_path / "a.db")
    a = reviews.add(reviews.validate(form(sign="no_company")), db_path=p)
    b = reviews.add(reviews.validate(form(sign="anon", name="Пётр")), db_path=p)
    c = reviews.add(reviews.validate(form(sign="что-то странное")), db_path=p)
    for rid in (a, b, c):
        reviews.set_status(rid, "approved", db_path=p)
    pub = {r["id"]: r for r in reviews.approved(p)}
    assert pub[a]["company"] == "" and pub[a]["name"] == "Иван"
    assert pub[b]["company"] == "" and "Пётр" not in pub[b]["name"] and pub[b]["role"] == "ГИП"
    assert pub[c]["company"] == "ООО Проект"              # неизвестный вариант → подпись полностью
    assert all("sign" not in r for r in pub.values())


def test_stars(tmp_path):
    p = str(tmp_path / "s.db")
    rid = reviews.add(reviews.validate(form(stars="4")), db_path=p)
    reviews.set_status(rid, "approved", db_path=p)
    assert reviews.approved(p)[0]["stars"] == 4
