"""Модели данных. Каждое извлечённое значение хранит ссылку на абзац-источник."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional, Union

Value = Union[int, date, str, None]


class DocType(str, Enum):
    CONTRACT = "договор"
    AMENDMENT = "дополнительное соглашение"
    DRAFT = "проект доп. соглашения"
    LETTER = "письмо"
    UNKNOWN = "тип не распознан"


class SignStatus(str, Enum):
    SIGNED = "подписан обеими сторонами"
    DRAFT = "проект / не подписан"
    PROPOSAL = "предложение (письмо)"
    UNKNOWN = "статус не распознан"


class ValueStatus(str, Enum):
    OK = "ok"
    NO_DATA = "нет данных"
    REVIEW = "требует проверки"


class ProposalKind(str, Enum):
    DRAFT = "проект, не подписан"
    LETTER = "предложение в письме"
    NOT_EFFECTIVE = "подписано, ещё не вступило в силу"


@dataclass(frozen=True)
class Paragraph:
    index: int          # порядковый номер логического абзаца, с 1
    text: str           # дословный фрагмент файла (может содержать переносы строк) — это цитата
    clause: str = ""    # номер пункта, если есть («3.1», «2»)

    @property
    def norm(self) -> str:
        """Текст с нормализованными пробелами — по нему работают правила извлечения."""
        return " ".join(self.text.split())


@dataclass(frozen=True)
class Evidence:
    filename: str
    paragraph: int
    quote: str
    clause: str = ""

    @property
    def label(self) -> str:
        where = f"п. {self.clause}" if self.clause else f"абзац {self.paragraph}"
        return f"{self.filename}, {where}"


@dataclass
class Document:
    filename: str
    text: str
    paragraphs: list[Paragraph]
    doc_type: DocType = DocType.UNKNOWN
    title: str = ""
    contract_number: Optional[str] = None
    status: SignStatus = SignStatus.UNKNOWN
    status_evidence: Optional[Evidence] = None
    doc_date: Optional[date] = None
    effective_date: Optional[date] = None
    effective_evidence: Optional[Evidence] = None
    effective_note: str = ""   # напр. «с даты подписания»
    warnings: list[str] = field(default_factory=list)

    def evidence(self, p: Paragraph) -> Evidence:
        return Evidence(self.filename, p.index, p.text, p.clause)


@dataclass
class Extraction:
    """Результат извлечения поля из одного фрагмента текста."""
    value: Value
    status: ValueStatus
    evidence: list[Evidence] = field(default_factory=list)
    note: str = ""
    candidates: list[tuple[Value, Evidence]] = field(default_factory=list)


@dataclass
class Step:
    """Шаг истории значения: исходный договор или применённое ДС."""
    value: Value
    status: ValueStatus
    evidence: list[Evidence]
    source: str  # «договор» / «ДС, в силе с 01.03.2026»


@dataclass
class FieldValue:
    value: Value
    status: ValueStatus
    evidence: list[Evidence] = field(default_factory=list)
    note: str = ""
    history: list[Step] = field(default_factory=list)
    candidates: list[tuple[Value, Evidence]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return len(self.history) > 1


@dataclass
class Proposal:
    contract_number: str
    field: str            # "amount" | "delivery"
    value: Value
    kind: ProposalKind
    evidence: Evidence
    note: str = ""


@dataclass
class ContractRow:
    number: str
    supplier: FieldValue
    amount: FieldValue
    delivery: FieldValue
    documents: list[Document] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def field(self, name: str) -> FieldValue:
        return getattr(self, name)

    def proposals_for(self, name: str) -> list[Proposal]:
        return [p for p in self.proposals if p.field == name]


@dataclass
class Orphan:
    """Документ, который не удалось привязать к загруженному договору."""
    document: Document
    reason: str


@dataclass
class Result:
    check_date: date
    rows: list[ContractRow]
    orphans: list[Orphan]
    documents: list[Document]
