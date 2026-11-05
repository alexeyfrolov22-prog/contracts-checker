"""Автотесты: сверка с эталоном test_data/expected.json (составлен вручную) + точечные проверки.

ID тест-кейсов совпадают с docs/test-cases.md.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime
from pathlib import Path

import pytest

from checker.extractors import words_to_int
from checker.models import DocType, SignStatus, ValueStatus
from checker.parser import detect_status, find_dates, parse_document, split_paragraphs
from checker.queries import deliveries_before, field_answer, group_documents
from checker.resolver import check

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = json.loads((ROOT / "test_data" / "expected.json").read_text(encoding="utf-8"))
CHECK_DATE = datetime.strptime(EXPECTED["check_date"], "%d.%m.%Y").date()


def load_dir(path: Path, order: str = "sorted") -> list[tuple[str, bytes]]:
    files = [(p.name, p.read_bytes()) for p in sorted(path.glob("*.txt"))]
    if order == "reversed":
        files.reverse()
    return files


def set_dir(name: str) -> Path:
    spec = EXPECTED["sets"][name]
    return ROOT / spec.get("dir", f"test_data/{name}")


def norm(v):
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    return v


def run(name: str, order: str = "reversed"):
    return check(load_dir(set_dir(name), order), CHECK_DATE)


# --- сверка с эталоном по всем наборам -------------------------------------

@pytest.mark.parametrize("name", list(EXPECTED["sets"]))
def test_matches_expected(name):
    spec = EXPECTED["sets"][name]
    result = run(name)
    rows = {r.number: r for r in result.rows}

    if "row_count" in spec:
        assert len(result.rows) == spec["row_count"]
    for number, fields in spec.get("contracts", {}).items():
        assert number in rows, f"{name}: нет строки {number}"
        row = rows[number]
        if "supplier" in fields:
            assert row.supplier.value == fields["supplier"]
        for f in ("amount", "delivery"):
            if f not in fields:
                continue
            exp, got = fields[f], row.field(f)
            ctx = f"{name} {number}.{f}: {got.note}"
            assert got.status.value == exp["status"], ctx
            if "value" in exp:
                assert norm(got.value) == exp["value"], ctx
            if "source" in exp:
                assert sorted({e.filename for e in got.evidence}) == sorted(exp["source"]), ctx
            if "was" in exp:
                assert norm(got.history[0].value) == exp["was"], ctx
            if "history" in exp:
                assert [norm(s.value) for s in got.history] == exp["history"], ctx
            if "candidates" in exp:
                assert sorted(norm(v) for v, _ in got.candidates) == sorted(exp["candidates"]), ctx

    if "proposals" in spec:
        got = sorted((p.contract_number, p.field, norm(p.value), p.kind.value, p.evidence.filename)
                     for r in result.rows for p in r.proposals)
        exp = sorted((p["contract"], p["field"], p["value"], p["kind"], p["source"]) for p in spec["proposals"])
        assert got == exp
    if "orphans" in spec:
        assert sorted(o.document.filename for o in result.orphans) == sorted(spec["orphans"])
    if "delivery_before" in spec:
        q = spec["delivery_before"]
        before = datetime.strptime(q["date"], "%d.%m.%Y").date()
        ans = deliveries_before(result, before)
        assert sorted(r.number for r, _ in ans.matches) == sorted(q["contracts"])
        assert sorted(r.number for r, props in ans.matches if props) == sorted(q["with_pending_term_change"])


# --- источники: цитата дословно есть в файле -------------------------------

@pytest.mark.parametrize("name", list(EXPECTED["sets"]))
def test_quotes_are_verbatim(name):
    """TC-19: каждая цитата посимвольно присутствует в исходном файле, номер абзаца совпадает."""
    result = run(name)
    docs = {d.filename: d for d in result.documents}
    for row in result.rows:
        for f in ("supplier", "amount", "delivery"):
            fv = row.field(f)
            for ev in fv.evidence + [e for _, e in fv.candidates]:
                doc = docs[ev.filename]
                assert ev.quote in doc.text
                assert doc.paragraphs[ev.paragraph - 1].text == ev.quote
        for p in row.proposals:
            assert p.evidence.quote in docs[p.evidence.filename].text


def test_values_present_in_quote():
    """Значение, которое видит пользователь, присутствует в его цитате (нет «выдуманных» чисел)."""
    result = run("base")
    for row in result.rows:
        for f in ("amount", "delivery"):
            fv = row.field(f)
            if fv.value is None:
                continue
            shown = (f"{fv.value:,}".replace(",", " ") if isinstance(fv.value, int)
                     else fv.value.strftime("%d.%m.%Y"))
            assert any(shown in e.quote for e in fv.evidence), (row.number, f)


# --- ловушки и статус документа --------------------------------------------

def test_traps_not_taken():
    """TC-04..TC-06, TC-13: аванс, срок оплаты, лимит, дата приёмки не попадают в значения."""
    result = run("base")
    rows = {r.number: r for r in result.rows}
    assert rows["STN-102"].amount.value != 252000
    assert rows["STN-102"].delivery.value != date(2026, 4, 10)
    assert rows["STN-103"].amount.value is None
    assert rows["STN-103"].amount.status is ValueStatus.NO_DATA


def test_draft_and_letter_do_not_change_values():
    """TC-09: проект ДС и письмо не меняют условия."""
    rows = {r.number: r for r in run("base").rows}
    assert rows["STN-101"].amount.value == 1380000
    assert rows["STN-102"].delivery.value == date(2026, 3, 20)


def test_check_date_before_amendment():
    """A4/A7: на дату до вступления ДС в силу (28.02.2026) действуют условия договора."""
    result = check(load_dir(ROOT / "documents"), date(2026, 2, 28))
    row = {r.number: r for r in result.rows}["STN-101"]
    assert row.delivery.value == date(2026, 4, 15)
    assert any(p.kind.value == "подписано, ещё не вступило в силу" for p in row.proposals)


@pytest.mark.parametrize("seed", range(5))
def test_order_independent(seed):
    """TC-16: порядок загрузки не влияет на результат."""
    files = load_dir(ROOT / "test_data" / "a3_later_wins") + load_dir(ROOT / "documents")
    random.Random(seed).shuffle(files)
    shuffled = check(files, CHECK_DATE)
    baseline = check(sorted(files), CHECK_DATE)
    summary = lambda r: [(x.number, x.amount.value, x.delivery.value, x.delivery.status) for x in r.rows]
    assert summary(shuffled) == summary(baseline)


# --- вопросы пользователя --------------------------------------------------

def test_questions_from_task():
    result = run("base")
    assert field_answer(result, "STN-101", "delivery").value.value == date(2026, 4, 30)
    assert field_answer(result, "STN-102", "amount").value.value == 840000
    q3 = field_answer(result, "STN-103", "amount").value
    assert q3.value is None and q3.status is ValueStatus.NO_DATA
    q4 = deliveries_before(result, date(2026, 4, 1))
    assert [r.number for r, _ in q4.matches] == ["STN-102"]
    assert [norm(p.value) for p in q4.matches[0][1]] == ["27.03.2026"]
    assert field_answer(result, "STN-999", "amount") is None


# --- пропись ---------------------------------------------------------------

@pytest.mark.parametrize("words,value", [
    ("один миллион двести тысяч", 1_200_000),
    ("один миллион триста восемьдесят тысяч", 1_380_000),
    ("восемьсот сорок тысяч", 840_000),
    ("четыреста пятьдесят тысяч", 450_000),
    ("с НДС", None),
])
def test_words_to_int(words, value):
    assert words_to_int(words) == value


# --- группировка документов по договорам (объединённая таблица UI) ----------

def _groups(name):
    return [(row.number if row else None, [d.filename for d in docs])
            for row, docs in group_documents(run(name))]


def test_group_documents_base():
    """TC-01: каждый файл показан под своим договором, договор — первым в группе."""
    assert _groups("base") == [
        ("STN-101", ["01_contract.txt", "02_amendment_signed.txt", "06_amendment_draft.txt"]),
        ("STN-102", ["03_contract.txt", "05_supplier_email.txt"]),
        ("STN-103", ["04_contract.txt"]),
    ]


@pytest.mark.parametrize("name,orphans", [
    ("tc18_orphan_amendment", ["02_amendment_signed.txt"]),   # TC-18
    ("tc20_encoding_empty", ["empty.txt"]),                   # TC-20
])
def test_group_documents_orphans(name, orphans):
    groups = _groups(name)
    assert groups[-1] == (None, orphans)
    grouped = [f for _, files in groups for f in files]
    assert len(grouped) == len(set(grouped)) == len(run(name).documents)  # каждый файл ровно один раз


# --- распознавание по всему тексту (robust_wording и точечные случаи) -------


def test_wrapped_lines_form_one_paragraph():
    """V06: пункт, разорванный переносом, — один абзац; цитата дословная, norm без переноса."""
    text = "2.1. Общая стоимость поставки составляет\n500 000 рублей, включая НДС.\n3.1. Срок поставки: до 15.04.2026.\n"
    paras = split_paragraphs(text)
    assert [p.clause for p in paras] == ["2.1", "3.1"]
    assert paras[0].text in text and "\n" in paras[0].text
    assert paras[0].norm == "2.1. Общая стоимость поставки составляет 500 000 рублей, включая НДС."


def test_letter_without_title_does_not_crash():
    """V10: раньше падало с AttributeError. Письмо распознаётся по смыслу и привязывается."""
    doc = parse_document("x.txt", "Уважаемые коллеги!\nПросим перенести срок поставки по договору № A-1 на 25.04.2026.\n".encode())
    assert doc.doc_type is DocType.LETTER and doc.contract_number == "A-1"


def test_unknown_document_does_not_crash():
    doc = parse_document("x.txt", "Счёт на оплату № 15 от 01.03.2026\nИтого: 10 000 рублей\n".encode())
    assert doc.doc_type is DocType.UNKNOWN and doc.warnings


@pytest.mark.parametrize("clause", [
    "5.1. Изменения действуют после подписания дополнительного соглашения обеими сторонами.",
    "5.1. Договор вступает в силу после подписания сторонами.",
    "5.1. Договор вступает в силу после подписания обеими сторонами.",
])
def test_signing_clause_is_not_a_signature(clause):
    """«после подписания (обеими) сторонами» — условие, а не факт подписания."""
    status, _, note = detect_status(split_paragraphs("ДОГОВОР ПОСТАВКИ № A-1\n" + clause))
    assert status is SignStatus.UNKNOWN and "не найдены" in note


@pytest.mark.parametrize("text,status", [
    ("Статус: подписан сторонами.", SignStatus.SIGNED),
    ("Статус: подписано Поставщиком 01.03.2026.", SignStatus.UNKNOWN),
    ("Статус: проект на согласовании.", SignStatus.DRAFT),
    ("Договор подписан обеими сторонами 05.02.2026.", SignStatus.SIGNED),
])
def test_status_phrases(text, status):
    assert detect_status(split_paragraphs("ДОГОВОР ПОСТАВКИ № A-1\n" + text))[0] is status


def test_letter_with_two_contract_numbers_is_not_linked():
    doc = parse_document("x.txt", "ПИСЬМО\nПросим перенести срок поставки по договору № A-1 и по договору № B-2.\n".encode())
    assert doc.contract_number is None and any("несколько договоров" in w for w in doc.warnings)


def test_amendment_number_is_not_contract_number():
    """«ДС № 1 К ДОГОВОРУ X» — номер договора X, а не 1."""
    doc = parse_document("x.txt", "ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ № 1 К ДОГОВОРУ № 45/2026\nСтатус: подписано обеими сторонами.\n".encode())
    assert doc.contract_number == "45/2026"


def test_effective_from_signing_date():
    """V08: «вступает в силу с даты его подписания» → дата подписания из статуса."""
    result = run("robust_wording")
    row = {r.number: r for r in result.rows}["STN-208"]
    assert "с даты подписания" in row.delivery.history[-1].source


@pytest.mark.parametrize("text,expected", [
    ("до 15 апреля 2026 г.", [date(2026, 4, 15)]),
    ("не позднее 5.4.2026", [date(2026, 4, 5)]),
    ("с 20.03.2026 на 27.03.2026", [date(2026, 3, 20), date(2026, 3, 27)]),
    ("31.02.2026", []),
])
def test_find_dates(text, expected):
    assert find_dates(text) == expected
