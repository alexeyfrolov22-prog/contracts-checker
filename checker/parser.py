"""Разбор TXT-документа: абзацы, тип, номер договора, статус подписи, даты.

Приоритет документа определяется только его содержимым (тип, статус, дата вступления
в силу). Имя файла и порядок загрузки не используются.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from .models import Document, DocType, Paragraph, SignStatus

DATE_RE = r"(\d{2}\.\d{2}\.\d{4})"
CONTRACT_NO_RE = re.compile(r"(?<![\w-])([A-ZА-ЯЁ]{2,}-\d+)(?![\w-])")
CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)*)\\?\.\s")
EFFECTIVE_RE = re.compile(r"вступает\s+в\s+силу\s+" + DATE_RE, re.IGNORECASE)
DOC_DATE_RE = re.compile(r"^Дата(?:\s+проекта)?:\s*" + DATE_RE)


def parse_date(s: str) -> Optional[date]:
    try:
        return datetime.strptime(s, "%d.%m.%Y").date()
    except ValueError:
        return None


def decode(raw: bytes) -> tuple[str, Optional[str]]:
    """UTF-8 (в т.ч. с BOM), при ошибке — cp1251. Возвращает (текст, предупреждение)."""
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace"), "файл прочитан в кодировке cp1251"


def split_paragraphs(text: str) -> list[Paragraph]:
    """Абзац = непустая строка. Нумерация с 1 — так её видит пользователь."""
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = CLAUSE_RE.match(line)
        result.append(Paragraph(len(result) + 1, line, m.group(1) if m else ""))
    return result


def detect_type(title: str) -> DocType:
    t = title.upper()
    if t.startswith("ПРОЕКТ"):
        return DocType.DRAFT
    if "ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ" in t:
        return DocType.AMENDMENT
    if t.startswith("ПИСЬМО"):
        return DocType.LETTER
    if "ДОГОВОР" in t:
        return DocType.CONTRACT
    return DocType.UNKNOWN


def detect_status(line: str) -> SignStatus:
    s = line.lower()
    if "не подписан" in s or "проект" in s:
        return SignStatus.DRAFT
    if re.search(r"подписан\w*\s+обеими\s+сторонами", s):
        return SignStatus.SIGNED
    return SignStatus.UNKNOWN


def parse_document(filename: str, raw: bytes) -> Document:
    text, enc_warning = decode(raw)
    paragraphs = split_paragraphs(text)
    doc = Document(filename=filename, text=text, paragraphs=paragraphs)
    if enc_warning:
        doc.warnings.append(enc_warning)
    if not paragraphs:
        doc.warnings.append("файл пустой")
        return doc

    doc.title = paragraphs[0].text
    doc.doc_type = detect_type(doc.title)
    if doc.doc_type is DocType.UNKNOWN:
        doc.warnings.append("тип документа не распознан по заголовку")

    # Номер договора: в заголовке, у письма — в строке «Тема:».
    m = CONTRACT_NO_RE.search(doc.title)
    if not m:
        for p in paragraphs:
            if p.text.lower().startswith("тема:"):
                m = CONTRACT_NO_RE.search(p.text)
                break
    if m:
        doc.contract_number = m.group(1)
    else:
        doc.warnings.append("номер договора не найден — документ не привязан")

    for p in paragraphs:
        if doc.doc_date is None and (dm := DOC_DATE_RE.match(p.text)):
            doc.doc_date = parse_date(dm.group(1))
        if p.text.lower().startswith("статус:") and doc.status_evidence is None:
            doc.status = detect_status(p.text)
            doc.status_evidence = doc.evidence(p)
        if doc.effective_date is None and (em := EFFECTIVE_RE.search(p.text)):
            doc.effective_date = parse_date(em.group(1))
            doc.effective_evidence = doc.evidence(p)

    if doc.doc_type is DocType.LETTER:
        # Письмо по правилу задания не меняет условий, независимо от формулировок.
        doc.status = SignStatus.PROPOSAL
    elif doc.doc_type is DocType.DRAFT:
        doc.status = SignStatus.DRAFT
    elif doc.status_evidence is None and doc.doc_type is not DocType.UNKNOWN:
        doc.warnings.append("строка «Статус:» не найдена")
    elif doc.status is SignStatus.UNKNOWN:
        doc.warnings.append(f"статус не распознан: «{doc.status_evidence.quote}»")

    if (doc.doc_type is DocType.AMENDMENT and doc.status is SignStatus.SIGNED
            and doc.effective_date is None):
        doc.warnings.append("дата вступления в силу не найдена")
    return doc
