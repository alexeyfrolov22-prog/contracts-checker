"""Ответы на вопросы пользователя. Вопросы параметризованы: ответы вычисляются из Result."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .models import ContractRow, Document, DocType, Evidence, FieldValue, Proposal, Result, Step, ValueStatus

FIELD_LABELS = {"amount": "Сумма договора", "delivery": "Срок поставки", "supplier": "Поставщик"}


def fmt_value(v, field_name: str = "") -> str:
    if v is None:
        return "нет данных"
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ") + " ₽"
    return str(v)


def find_row(result: Result, number: str) -> Optional[ContractRow]:
    return next((r for r in result.rows if r.number == number), None)


@dataclass
class FieldAnswer:
    row: ContractRow
    field: str
    value: FieldValue
    proposals: list[Proposal]


def field_answer(result: Result, number: str, field_name: str) -> Optional[FieldAnswer]:
    row = find_row(result, number)
    if row is None:
        return None
    return FieldAnswer(row, field_name, row.field(field_name), row.proposals_for(field_name))


@dataclass
class EarlyDeliveries:
    before: date
    matches: list[tuple[ContractRow, list[Proposal]]]   # договор + несогласованные изменения срока
    review: list[ContractRow]                            # срок не установлен надёжно
    all_pending: list[Proposal] = field(default_factory=list)


def deliveries_before(result: Result, before: date) -> EarlyDeliveries:
    """Q4: договоры со сроком поставки раньше даты + несогласованные изменения срока."""
    matches, review = [], []
    for row in result.rows:
        fv = row.delivery
        if fv.status is ValueStatus.OK and isinstance(fv.value, date) and fv.value < before:
            matches.append((row, row.proposals_for("delivery")))
        elif fv.status is ValueStatus.REVIEW:
            review.append(row)
    pending = [p for row in result.rows for p in row.proposals_for("delivery")]
    return EarlyDeliveries(before, matches, review, pending)


_TYPE_ORDER = {DocType.CONTRACT: 0, DocType.AMENDMENT: 1, DocType.DRAFT: 2, DocType.LETTER: 3, DocType.UNKNOWN: 4}


def _doc_key(d: Document):
    # Только порядок отображения; на расчёт значений не влияет.
    return (_TYPE_ORDER[d.doc_type], d.doc_date or date.max, d.filename)


def group_documents(result: Result) -> list[tuple[Optional[ContractRow], list[Document]]]:
    """Загруженные документы, сгруппированные по договорам; последняя группа (None) — не привязанные."""
    groups: list[tuple[Optional[ContractRow], list[Document]]] = [
        (row, sorted(row.documents, key=_doc_key)) for row in result.rows]
    if result.orphans:
        groups.append((None, sorted((o.document for o in result.orphans), key=_doc_key)))
    return groups


_AMENDMENT_NO = re.compile(r"соглашени\w*\s+№\s*([\w./-]+)", re.IGNORECASE)


def doc_label(d: Document, with_date: bool = True) -> str:
    """Человекочитаемое название: «ДС № 1 от 01.03.2026», «Письмо от 05.03.2026». Имя файла — запасной вариант.
    with_date=False — без даты (когда дата уже показана рядом, например в таймлайне)."""
    when = f" от {d.doc_date.strftime('%d.%m.%Y')}" if d.doc_date and with_date else ""
    if d.doc_type is DocType.CONTRACT:
        return f"Договор{' № ' + d.contract_number if d.contract_number else ''}{when}"
    if d.doc_type in (DocType.AMENDMENT, DocType.DRAFT):
        m = _AMENDMENT_NO.search(d.title)
        name = ("Проект ДС" if d.doc_type is DocType.DRAFT else "ДС") + (f" № {m.group(1)}" if m else "")
        return name + when
    if d.doc_type is DocType.LETTER:
        return "Письмо" + when
    return d.filename


# --- таймлайн значения поля (для истории договора) ---------------------------

ROLE_WAS, ROLE_CURRENT, ROLE_DOUBT = "было", "действует", "под вопросом"
ROLE_NO_DATA, ROLE_REVIEW, ROLE_PENDING = "нет данных", "требует проверки", "не согласовано"


@dataclass
class TimelineEvent:
    date: Optional[date]
    document: Document
    role: str
    value: object
    evidence: Evidence
    note: str = ""


def field_timeline(row: ContractRow, field_name: str, docs: dict[str, Document]) -> list[TimelineEvent]:
    """События по документам: что было, что действует, что не согласовано или требует проверки.

    Строится из уже посчитанных history / candidates / proposals — новой логики расчёта нет.
    """
    fv = row.field(field_name)
    events: list[TimelineEvent] = []

    def add(ev: Evidence, role: str, value, note: str = "") -> None:
        d = docs[ev.filename]
        notes = [note] if note else []
        if d.effective_date and d.effective_date != d.doc_date and role != ROLE_PENDING:
            notes.append(f"в силе с {d.effective_date.strftime('%d.%m.%Y')}")
        events.append(TimelineEvent(d.doc_date or d.effective_date, d, role, value, ev, "; ".join(notes)))

    steps = fv.history or [Step(fv.value, fv.status, fv.evidence, "договор")]
    for i, step in enumerate(steps):
        if step.value is None and step.status is ValueStatus.REVIEW and fv.candidates:
            continue  # конфликт: вместо пустого шага показываются кандидаты
        if i < len(steps) - 1:
            role = ROLE_WAS
        else:
            role = {ValueStatus.OK: ROLE_CURRENT, ValueStatus.REVIEW: ROLE_DOUBT}.get(fv.status, ROLE_NO_DATA)
        for ev in step.evidence:
            add(ev, role, step.value)
    for value, ev in fv.candidates:
        add(ev, ROLE_REVIEW, value)
    for p in row.proposals_for(field_name):
        add(p.evidence, ROLE_PENDING, p.value, p.kind.value + (f", {p.note}" if p.note else ""))
    # По дате документа; события без даты — в конце. sorted() устойчив: порядок внутри даты сохраняется.
    return sorted(events, key=lambda e: (e.date is None, e.date or date.min))
