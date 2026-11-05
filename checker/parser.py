"""Разбор TXT-документа: абзацы, тип, номер договора, статус подписи, даты.

Признаки ищутся по смыслу во всём тексте, а не по позиции строки: заголовок может стоять
не в первой строке, строки «Статус:» может не быть, пункт может быть разбит переносами.
Приоритет документа определяется только содержимым (тип, статус, дата вступления в силу);
имя файла и порядок загрузки не используются.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .models import Document, DocType, Paragraph, SignStatus

# --- даты --------------------------------------------------------------------

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
          "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_MONTHS_RE = "|".join(MONTHS)
# Строковый шаблон даты без групп — для встраивания в другие выражения.
DATE_ANY = rf"(?:\d{{1,2}}\.\d{{1,2}}\.\d{{4}}|\d{{1,2}}\s+(?:{_MONTHS_RE})\s+\d{{4}}(?:\s*(?:года|г\.?))?)"
_DATE_NUM = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_DATE_WORD = re.compile(rf"(\d{{1,2}})\s+({_MONTHS_RE})\s+(\d{{4}})", re.IGNORECASE)
_DATE_FIND = re.compile(rf"(?<![\d.]){DATE_ANY}(?![\d])", re.IGNORECASE)


def parse_date(s: str) -> Optional[date]:
    """«15.04.2026», «5.4.2026», «15 апреля 2026 г.» → date; иначе None."""
    try:
        if m := _DATE_NUM.search(s):
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if m := _DATE_WORD.search(s):
            return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None
    return None


def find_dates(text: str) -> list[date]:
    return [d for d in (parse_date(m.group(0)) for m in _DATE_FIND.finditer(text)) if d]


# --- абзацы ------------------------------------------------------------------

CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)*)\\?\.\s")
HEADER_RE = re.compile(r"^[А-ЯЁA-Z][А-Яа-яЁёA-Za-z ]{1,25}:\s")
SENTENCE_END = tuple(".!?;:»")


def _is_upper_title(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    return len(letters) >= 4 and all(c.isupper() for c in letters)


def split_paragraphs(text: str) -> list[Paragraph]:
    """Абзац = логический пункт. Строки, разорванные переносом, склеиваются.

    Новый абзац начинается после пустой строки, со строки с номером пункта, со строки вида
    «Слово:», со строки заглавными буквами или если предыдущая строка закончила предложение.
    `Paragraph.text` — дословный фрагмент файла (с переносами), поэтому цитата точная.
    """
    spans: list[list[int]] = []          # [start, end] в исходном тексте
    prev_line = ""
    offset = 0
    for raw in text.splitlines(keepends=True):
        line_start, offset = offset, offset + len(raw)
        line = raw.strip()
        if not line:
            prev_line = ""
            continue
        start = line_start + (len(raw) - len(raw.lstrip()))
        end = line_start + len(raw.rstrip())
        new = (not prev_line or CLAUSE_RE.match(line) or HEADER_RE.match(line) or _is_upper_title(line)
               or _is_upper_title(prev_line) or prev_line.endswith(SENTENCE_END))
        if new:
            spans.append([start, end])
        else:
            spans[-1][1] = end
        prev_line = line
    result = []
    for i, (s, e) in enumerate(spans, 1):
        chunk = text[s:e]
        m = CLAUSE_RE.match(chunk)
        result.append(Paragraph(i, chunk, m.group(1) if m else ""))
    return result


# --- тип документа -----------------------------------------------------------

TITLE_PATTERNS = [
    (DocType.DRAFT, re.compile(r"^проект\s+(?:дополнительного\s+)?соглашени", re.IGNORECASE)),
    (DocType.AMENDMENT, re.compile(r"^дополнительное\s+соглашение", re.IGNORECASE)),
    (DocType.LETTER, re.compile(r"^письмо\b", re.IGNORECASE)),
    (DocType.CONTRACT, re.compile(r"^договор\s+(?:поставки|№)", re.IGNORECASE)),
]
LETTER_HINTS = re.compile(r"^(?:от|кому|тема):|уважаем\w+|с\s+уважением|\bпросим\b|\bпредлагаем\b",
                          re.IGNORECASE)
TITLE_ZONE = 5  # заголовок ищется в первых абзацах, а не только в первой строке


def detect_type(paragraphs: list[Paragraph]) -> tuple[DocType, Optional[Paragraph]]:
    for p in paragraphs[:TITLE_ZONE]:
        for doc_type, rx in TITLE_PATTERNS:
            if rx.search(p.norm):
                return doc_type, p
    if any(LETTER_HINTS.search(p.norm) for p in paragraphs):
        return DocType.LETTER, None
    return DocType.UNKNOWN, None


# --- номер договора ------------------------------------------------------------

_NUM = r"([A-Za-zА-Яа-яЁё0-9](?:[\w./-]*\w)?)"
CONTRACT_NO_TITLE = re.compile(rf"договор\w*\s+(?:поставки\s+)?№\s*{_NUM}", re.IGNORECASE)
TO_CONTRACT_NO = re.compile(rf"к\s+договору\s+(?:поставки\s+)?(?:№\s*)?{_NUM}", re.IGNORECASE)
ANY_CONTRACT_NO = re.compile(rf"договор\w*\s+(?:поставки\s+)?(?:№\s*)?{_NUM}", re.IGNORECASE)


def _valid_number(s: str) -> bool:
    return any(c.isdigit() for c in s) and parse_date(s) is None


def _numbers(rx: re.Pattern, paragraphs: list[Paragraph]) -> list[str]:
    found = []
    for p in paragraphs:
        for m in rx.finditer(p.norm):
            n = m.group(1)
            if _valid_number(n) and n not in found:
                found.append(n)
    return found


def detect_number(doc_type: DocType, title: Optional[Paragraph],
                  paragraphs: list[Paragraph]) -> tuple[Optional[str], Optional[str]]:
    """Возвращает (номер, предупреждение)."""
    if doc_type is DocType.CONTRACT:
        nums = _numbers(CONTRACT_NO_TITLE, [title] if title else []) or _numbers(CONTRACT_NO_TITLE, paragraphs)
        nums = nums[:1]  # договор — по своему заголовку; ссылки на другие договоры в тексте не важны
    elif doc_type in (DocType.AMENDMENT, DocType.DRAFT):
        nums = _numbers(TO_CONTRACT_NO, paragraphs)
    else:
        nums = _numbers(ANY_CONTRACT_NO, paragraphs)
    if not nums:
        return None, "номер договора не найден — документ не привязан"
    if len(nums) > 1:
        return None, f"документ ссылается на несколько договоров ({', '.join(nums)}) — не привязан"
    return nums[0], None


# --- статус подписания -------------------------------------------------------

SIGNED_RE = re.compile(r"подписан[оаы]?\s+(?:обеими\s+)?сторонами|стороны\s+подписали", re.IGNORECASE)
DRAFT_RE = re.compile(r"\bне\s+подписан|\bпроект\b|на\s+согласовании", re.IGNORECASE)
PARTIAL_RE = re.compile(r"подписан[оаы]?\s+(?:только\s+)?(?:поставщиком|покупателем|одной\s+стороной)",
                        re.IGNORECASE)


def classify_status(text: str) -> Optional[SignStatus]:
    """Статус по фрагменту текста; None — признаков нет. «подписания» признаком не считается."""
    if PARTIAL_RE.search(text):
        return SignStatus.UNKNOWN
    if DRAFT_RE.search(text):
        return SignStatus.DRAFT
    if SIGNED_RE.search(text):
        return SignStatus.SIGNED
    return None


def detect_status(paragraphs: list[Paragraph]) -> tuple[SignStatus, Optional[Paragraph], str]:
    """Сначала строка «Статус:», при её отсутствии — весь текст. Возвращает (статус, абзац, замечание)."""
    status_lines = [p for p in paragraphs if p.norm.lower().startswith("статус:")]
    if status_lines:
        p = status_lines[0]
        st = classify_status(p.norm)
        if st in (None, SignStatus.UNKNOWN):
            return SignStatus.UNKNOWN, p, f"статус не распознан: «{p.norm}»"
        return st, p, ""
    signals = [(classify_status(p.norm), p) for p in paragraphs]
    signals = [(s, p) for s, p in signals if s is not None]
    kinds = {s for s, _ in signals}
    if not signals:
        return SignStatus.UNKNOWN, None, "признаки подписания в тексте не найдены"
    if len(kinds) > 1 or SignStatus.UNKNOWN in kinds:
        return SignStatus.UNKNOWN, signals[0][1], "противоречивые признаки подписания"
    return signals[0][0], signals[0][1], ""


# --- дата вступления в силу ----------------------------------------------------

EFFECTIVE_RE = re.compile(rf"(?:вступа\w*\s+в\s+силу\s+(?:с\s+)?|действует\s+с\s+)({DATE_ANY})", re.IGNORECASE)
EFFECTIVE_ON_SIGN_RE = re.compile(
    r"вступа\w*\s+в\s+силу\s+с\s+(?:даты|момента)\s+(?:его\s+|её\s+|ее\s+)?подписания", re.IGNORECASE)
DOC_DATE_RE = re.compile(rf"^Дата(?:\s+\w+)?:\s*({DATE_ANY})", re.IGNORECASE)


def parse_document(filename: str, raw: bytes) -> Document:
    text, enc_warning = decode(raw)
    paragraphs = split_paragraphs(text)
    doc = Document(filename=filename, text=text, paragraphs=paragraphs)
    if enc_warning:
        doc.warnings.append(enc_warning)
    if not paragraphs:
        doc.warnings.append("файл пустой")
        return doc

    doc.doc_type, title = detect_type(paragraphs)
    doc.title = (title or paragraphs[0]).norm
    if doc.doc_type is DocType.UNKNOWN:
        doc.warnings.append("тип документа не распознан (договор, доп. соглашение, проект или письмо)")
        return doc

    doc.contract_number, warn = detect_number(doc.doc_type, title, paragraphs)
    if warn:
        doc.warnings.append(warn)

    for p in paragraphs:
        if doc.doc_date is None and (dm := DOC_DATE_RE.match(p.norm)):
            doc.doc_date = parse_date(dm.group(1))

    if doc.doc_type is DocType.LETTER:
        doc.status = SignStatus.PROPOSAL      # письмо не меняет условий (правило задания)
    elif doc.doc_type is DocType.DRAFT:
        doc.status = SignStatus.DRAFT
        doc.status_evidence = doc.evidence(title) if title else None
    else:
        doc.status, sp, note = detect_status(paragraphs)
        doc.status_evidence = doc.evidence(sp) if sp else None
        if note:
            doc.warnings.append(note)

    if doc.doc_type is DocType.AMENDMENT:
        for p in paragraphs:
            if em := EFFECTIVE_RE.search(p.norm):
                doc.effective_date, doc.effective_evidence = parse_date(em.group(1)), doc.evidence(p)
                break
            if EFFECTIVE_ON_SIGN_RE.search(p.norm):
                signed_on = (find_dates(doc.status_evidence.quote) if doc.status_evidence else []) or (
                    [doc.doc_date] if doc.doc_date else [])
                doc.effective_evidence = doc.evidence(p)
                if signed_on:
                    doc.effective_date, doc.effective_note = signed_on[0], "с даты подписания"
                break
        if doc.status is SignStatus.SIGNED and doc.effective_date is None:
            doc.warnings.append("дата вступления в силу не найдена")
    return doc


def decode(raw: bytes) -> tuple[str, Optional[str]]:
    """UTF-8 (в т.ч. с BOM), при ошибке — cp1251. Возвращает (текст, предупреждение)."""
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace"), "файл прочитан в кодировке cp1251"
