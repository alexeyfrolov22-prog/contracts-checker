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
from checker.models import ValueStatus
from checker.queries import deliveries_before, field_answer
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
