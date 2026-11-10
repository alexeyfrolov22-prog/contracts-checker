"""Сборка условий договора на дату проверки.

Правило задания: условия меняет только дополнительное соглашение, подписанное обеими
сторонами и вступившее в силу к дате проверки. Проект ДС, письмо и подписанное, но ещё
не вступившее в силу ДС показываются отдельно и значений в таблице не меняют.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date
from itertools import groupby

from .extractors import EXTRACTORS, extract_supplier
from .models import (ContractRow, Document, DocType, Extraction, FieldValue, Orphan,
                     Proposal, ProposalKind, Result, SignStatus, Step, ValueStatus)
from .parser import parse_document

FIELDS = ("amount", "delivery")


def fmt_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


def load(files: list[tuple[str, bytes]]) -> list[Document]:
    """Разбор загруженных файлов; одинаковые по содержимому файлы учитываются один раз."""
    docs, seen = [], {}
    for name, raw in files:
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            seen[digest].warnings.append(f"дубликат загружен как «{name}» и не учитывается повторно")
            continue
        doc = parse_document(name, raw)
        seen[digest] = doc
        docs.append(doc)
    return docs


def _base_field(ex: Extraction, contract: Document) -> FieldValue:
    fv = FieldValue(ex.value, ex.status, ex.evidence, ex.note, candidates=ex.candidates)
    fv.history.append(Step(ex.value, ex.status, ex.evidence, "договор"))
    if contract.status is not SignStatus.SIGNED and fv.status is ValueStatus.OK:
        fv.status = ValueStatus.REVIEW
        fv.note = f"статус договора не подтверждён ({contract.status.value})"
    return fv


def _apply(fv: FieldValue, changes: list[tuple[date, Extraction, Document]]) -> None:
    """Применяет действующие ДС по дате вступления в силу (не по имени файла и не по порядку)."""
    changes.sort(key=lambda c: c[0])
    for eff, group in groupby(changes, key=lambda c: c[0]):
        group = list(group)
        values = []
        for _, ex, _ in group:
            if ex.value not in values:
                values.append(ex.value)
        evidence = [e for _, ex, _ in group for e in ex.evidence]
        eff_notes = sorted({d.effective_note for _, _, d in group if d.effective_note})
        source = f"ДС, в силе с {fmt_date(eff)}" + (f" ({', '.join(eff_notes)})" if eff_notes else "")
        if len(values) > 1:
            fv.value, fv.status, fv.evidence = None, ValueStatus.REVIEW, evidence
            fv.candidates = [(ex.value, ex.evidence[0]) for _, ex, _ in group]
            fv.note = (f"несколько подписанных ДС с одной датой вступления в силу ({fmt_date(eff)}) "
                       f"дают разные значения")
            fv.history.append(Step(None, ValueStatus.REVIEW, evidence, source))
            continue
        ex = group[0][1]
        notes = "; ".join(e.note for _, e, _ in group if e.note)
        fv.value, fv.status, fv.evidence, fv.note, fv.candidates = (
            values[0], ex.status, evidence, notes, [])
        fv.history.append(Step(values[0], ex.status, evidence, source))


def build_row(number: str, related: list[Document], check_date: date) -> ContractRow:
    contracts = [d for d in related if d.doc_type is DocType.CONTRACT]
    contract = contracts[0]
    supplier = extract_supplier(contract)
    row = ContractRow(
        number=number,
        supplier=FieldValue(supplier.value, supplier.status, supplier.evidence, supplier.note),
        amount=_base_field(EXTRACTORS["amount"](contract), contract),
        delivery=_base_field(EXTRACTORS["delivery"](contract), contract),
        documents=related,
    )
    if len(contracts) > 1:
        names = ", ".join(d.filename for d in contracts)
        row.warnings.append(f"несколько договоров с номером {number}: {names}")
        for f in FIELDS:
            row.field(f).status = ValueStatus.REVIEW
            row.field(f).note = "загружено несколько договоров с одним номером"

    applied: dict[str, list] = defaultdict(list)
    unclear: dict[str, list] = defaultdict(list)  # ДС, применимость которых не установлена
    for doc in related:
        if doc.doc_type in (DocType.CONTRACT, DocType.UNKNOWN):
            continue
        for f in FIELDS:
            ex = EXTRACTORS[f](doc)
            if ex.value is None:
                continue
            ev = ex.evidence[0]
            if doc.doc_type is DocType.LETTER:
                row.proposals.append(Proposal(number, f, ex.value, ProposalKind.LETTER, ev))
            elif doc.doc_type is DocType.DRAFT or doc.status is SignStatus.DRAFT:
                row.proposals.append(Proposal(number, f, ex.value, ProposalKind.DRAFT, ev))
            elif doc.status is SignStatus.UNKNOWN:
                unclear[f].append((ex, f"статус ДС не распознан ({doc.filename})"))
            elif doc.effective_date is None:
                unclear[f].append((ex, f"у подписанного ДС нет даты вступления в силу ({doc.filename})"))
            elif doc.effective_date > check_date:
                row.proposals.append(Proposal(number, f, ex.value, ProposalKind.NOT_EFFECTIVE, ev,
                                              f"вступает в силу {fmt_date(doc.effective_date)}"))
            else:
                applied[f].append((doc.effective_date, ex, doc))

    for f in FIELDS:
        fv = row.field(f)
        if applied[f]:
            _apply(fv, applied[f])
        for ex, reason in unclear[f]:
            fv.status = ValueStatus.REVIEW
            fv.note = "; ".join(filter(None, [fv.note, reason]))
            fv.candidates = fv.candidates + [(ex.value, ex.evidence[0])]
    return row


def resolve(docs: list[Document], check_date: date) -> Result:
    by_number: dict[str, list[Document]] = defaultdict(list)
    orphans: list[Orphan] = []
    for d in docs:
        if not d.paragraphs:
            orphans.append(Orphan(d, "файл пустой"))
        elif d.doc_type is DocType.UNKNOWN:
            orphans.append(Orphan(d, "тип документа не распознан"))
        elif not d.contract_number:
            orphans.append(Orphan(d, "номер договора не найден — документ не привязан"))
        else:
            by_number[d.contract_number].append(d)

    rows = []
    for number in sorted(by_number):
        related = by_number[number]
        if not any(d.doc_type is DocType.CONTRACT for d in related):
            for d in related:
                orphans.append(Orphan(d, f"договор {number} не загружен"))
            continue
        rows.append(build_row(number, related, check_date))
    return Result(check_date, rows, orphans, docs)


def check(files: list[tuple[str, bytes]], check_date: date) -> Result:
    return resolve(load(files), check_date)
