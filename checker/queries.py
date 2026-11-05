"""Ответы на вопросы пользователя. Вопросы параметризованы: ответы вычисляются из Result."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .models import ContractRow, FieldValue, Proposal, Result, ValueStatus

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
