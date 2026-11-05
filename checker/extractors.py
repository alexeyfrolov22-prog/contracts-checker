"""Правила извлечения полей. Значение возвращается только вместе с абзацем-источником.

Правила работают по нормализованному тексту абзаца (`Paragraph.norm`), цитата берётся
дословно из файла (`Paragraph.text`). Если однозначного значения нет — «нет данных»
или «требует проверки», но не догадка.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional

from .models import Document, Evidence, Extraction, Paragraph, ValueStatus
from .parser import DATE_ANY, find_dates, parse_date

# --- общие утилиты ---------------------------------------------------------

REDACTION_RE = re.compile(r"в\s+следующей\s+редакции\s*:\s*«", re.IGNORECASE)
HEADER_PREFIXES = ("дата:", "дата проекта:", "статус:", "от:", "кому:", "тема:")


def redaction_text(text: str) -> Optional[str]:
    """Текст новой редакции пункта: «…» после «в следующей редакции:»."""
    m = REDACTION_RE.search(text)
    if not m:
        return None
    end = text.rfind("»")
    return text[m.end():end] if end > m.end() else text[m.end():]


def is_header(p: Paragraph) -> bool:
    return p.norm.lower().startswith(HEADER_PREFIXES)


def sentences(text: str) -> list[str]:
    # Точка внутри даты или номера пункта не разрывает предложение.
    return [s for s in re.split(r"(?<=[.!?»])\s+(?=[А-ЯЁA-Z«])", text) if s.strip()]


def unique(values: Iterable) -> list:
    seen = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


# --- сумма -----------------------------------------------------------------

MONEY_RE = re.compile(
    r"(?<![\d.,])(\d+(?:[  ]\d{3})*)(?:,\d{1,2})?\s*(?:\(([^()]*)\)\s*)?(?:руб|₽|RUB\b|р\.)",
    re.IGNORECASE,
)
PRICE_KEY_RE = re.compile(
    r"(?:общ\w*|итогов\w*)\s+(?:стоимост|сумм|цен)\w*|цен\w*\s+(?:договора|товара)"
    r"|сумм\w*\s+договора|стоимост\w*\s+договора",
    re.IGNORECASE,
)
# Суммы, которые не являются ценой договора.
PRICE_EXCLUDE_RE = re.compile(r"аванс|лимит|бюджет|не\s+является|неустой|штраф|пени", re.IGNORECASE)

_UNITS = {
    "ноль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
    "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
    "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
    "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100, "двести": 200,
    "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600, "семьсот": 700,
    "восемьсот": 800, "девятьсот": 900,
}
_SCALES = {
    "тысяча": 10**3, "тысячи": 10**3, "тысяч": 10**3,
    "миллион": 10**6, "миллиона": 10**6, "миллионов": 10**6,
    "миллиард": 10**9, "миллиарда": 10**9, "миллиардов": 10**9,
}


def words_to_int(words: str) -> Optional[int]:
    """«один миллион двести тысяч» → 1200000. Неизвестное слово → None (сверка не делается)."""
    tokens = re.findall(r"[а-яё]+", words.lower())
    if not tokens:
        return None
    total = current = 0
    for t in tokens:
        if t in _UNITS:
            current += _UNITS[t]
        elif t in _SCALES:
            total += (current or 1) * _SCALES[t]
            current = 0
        else:
            return None
    return total + current


def _money_in(text: str) -> list[tuple[int, Optional[str]]]:
    out = []
    for m in MONEY_RE.finditer(text):
        digits = int(re.sub(r"\D", "", m.group(1)))
        out.append((digits, m.group(2)))
    return out


def amount_from_text(text: str) -> tuple[list[int], list[str]]:
    """Суммы цены договора во фрагменте. Возвращает (значения, замечания о расхождении с прописью)."""
    values, notes = [], []
    for sent in sentences(text):
        if not PRICE_KEY_RE.search(sent) or PRICE_EXCLUDE_RE.search(sent):
            continue
        for digits, words in _money_in(sent):
            values.append(digits)
            if words is not None:
                parsed = words_to_int(words)
                if parsed is not None and parsed != digits:
                    notes.append(f"цифры ≠ пропись: {digits:,} и «{words}» ({parsed:,})".replace(",", " "))
    return values, notes


def is_price_paragraph(text: str) -> bool:
    return any(PRICE_KEY_RE.search(s) and not PRICE_EXCLUDE_RE.search(s) for s in sentences(text))


# --- срок поставки ---------------------------------------------------------

DELIVERY_KEY_RE = re.compile(r"постав(?:к|ит|л)|срок\w*\s+поставки", re.IGNORECASE)
DELIVERY_EXCLUDE_RE = re.compile(r"оплат|при[её]мк|по\s+состоянию\s+на", re.IGNORECASE)
# Порядок важен: «на DATE» (новая дата при переносе) → «не позднее» → «до/по» → «срок поставки: DATE».
DATE_MARKERS = [re.compile(prefix + rf"\s*({DATE_ANY})", re.IGNORECASE) for prefix in (
    r"\bна", r"не\s+позднее", r"\b(?:до|по)", r"срок\w*\s+поставки\s*[:—–-]")]
RELATIVE_RE = re.compile(
    r"в\s+течение\s+\d+|\d+\s+(?:\(\w+\)\s+)?(?:рабоч\w+\s+|календарн\w+\s+)?(?:дн|недел|месяц)",
    re.IGNORECASE)


def _delivery_sentences(text: str) -> list[str]:
    return [s for s in sentences(text) if DELIVERY_KEY_RE.search(s) and not DELIVERY_EXCLUDE_RE.search(s)]


def delivery_from_text(text: str) -> list[date]:
    found: list[Optional[date]] = []
    for sent in _delivery_sentences(text):
        dates = find_dates(sent)
        if not dates:
            continue
        for rx in DATE_MARKERS:
            if m := rx.search(sent):
                found.append(parse_date(m.group(1)))
                break
        else:
            found.extend(dates)
    return [d for d in found if d]


def is_delivery_paragraph(text: str) -> bool:
    return any(re.search(r"срок|не\s+позднее", s, re.IGNORECASE) for s in _delivery_sentences(text))


def is_relative_delivery(text: str) -> bool:
    """Срок задан относительно события («в течение 30 дней с даты подписания») — дату не вычислить."""
    return any(RELATIVE_RE.search(s) and not find_dates(s) for s in _delivery_sentences(text))


# --- извлечение из документа ----------------------------------------------

def _resolve(cands: list[tuple[object, Evidence]], mentions: list[Evidence],
             notes: list[str], missing_note: str) -> Extraction:
    values = unique(v for v, _ in cands)
    if not values:
        return Extraction(None, ValueStatus.NO_DATA, mentions,
                          "числовое значение в найденном пункте не указано" if mentions else missing_note)
    evidence = unique(e for _, e in cands)
    if len(values) > 1:
        return Extraction(None, ValueStatus.REVIEW, evidence,
                          "в документе несколько разных значений", candidates=cands)
    if notes:
        return Extraction(values[0], ValueStatus.REVIEW, evidence, "; ".join(notes))
    return Extraction(values[0], ValueStatus.OK, evidence)


def extract_amount(doc: Document) -> Extraction:
    cands, mentions, notes = [], [], []
    for p in doc.paragraphs:
        if is_header(p):
            continue
        text = redaction_text(p.norm) or p.norm
        vals, n = amount_from_text(text)
        cands += [(v, doc.evidence(p)) for v in vals]
        notes += n
        if not vals and is_price_paragraph(text):
            mentions.append(doc.evidence(p))
    return _resolve(cands, mentions, notes, "в тексте нет пункта о стоимости договора")


def extract_delivery(doc: Document) -> Extraction:
    cands, mentions, relative = [], [], []
    for p in doc.paragraphs:
        if is_header(p):
            continue
        text = redaction_text(p.norm) or p.norm
        vals = delivery_from_text(text)
        cands += [(v, doc.evidence(p)) for v in vals]
        if not vals and is_relative_delivery(text):
            relative.append(doc.evidence(p))
        elif not vals and is_delivery_paragraph(text):
            mentions.append(doc.evidence(p))
    if not cands and relative:
        return Extraction(None, ValueStatus.REVIEW, relative,
                          "срок поставки задан относительно события — календарную дату вычислить нельзя")
    return _resolve(cands, mentions, [], "в тексте нет пункта о сроке поставки")


SUPPLIER_RE = re.compile(r"Поставщик:\s*((?:[А-ЯЁA-Z]{2,4}\s*)?«[^»]+»|[^.;]+)")


def extract_supplier(doc: Document) -> Extraction:
    for p in doc.paragraphs:
        m = SUPPLIER_RE.search(p.norm)
        if m:
            return Extraction(m.group(1).strip(), ValueStatus.OK, [doc.evidence(p)])
    return Extraction(None, ValueStatus.NO_DATA, [], "поставщик в тексте не указан")


EXTRACTORS = {"amount": extract_amount, "delivery": extract_delivery}
