"""Streamlit-интерфейс для закупщика. Логика извлечения — в пакете checker/."""
from __future__ import annotations

import html
from datetime import date

import pandas as pd
import streamlit as st

from checker.models import Evidence, FieldValue, Proposal, Result, ValueStatus
from checker.queries import FIELD_LABELS, deliveries_before, field_answer, fmt_value
from checker.resolver import check, fmt_date

DEFAULT_CHECK_DATE = date(2026, 3, 10)

FORMAT_HINT = """
**Требования к формату документов.** Прототип работает на правилах, без LLM, и распознаёт только такой формат:
- в первой строке указан тип документа: `ДОГОВОР ПОСТАВКИ`, `ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ`, `ПРОЕКТ ДОПОЛНИТЕЛЬНОГО СОГЛАШЕНИЯ` или `ПИСЬМО`;
- номер договора записан в виде `БУКВЫ-ЦИФРЫ` (например, `STN-101`) в первой строке, а у письма — в строке «Тема:»;
- есть строка `Статус:` с формулировкой «подписан(о) обеими сторонами» или «проект / не подписан»;
- в доп. соглашении есть фраза «вступает в силу ДД.ММ.ГГГГ»;
- даты записаны как `ДД.ММ.ГГГГ`, суммы — цифрами с пробелами и словом «рублей»; кодировка UTF-8 или cp1251.

Если документ не соответствует формату, приложение не подставляет значение: документ получает предупреждение или статус «требует проверки».
"""

CSS = """
<style>
.src-p {padding: 2px 8px; border-left: 3px solid transparent; white-space: pre-wrap;
        font-family: ui-monospace, monospace; font-size: .9rem; scroll-margin-top: 90px;}
.src-p .pn {opacity: .55; margin-right: .6em; user-select: none;}
.src-p.ref {background: rgba(255, 200, 0, .16); border-left-color: #e0a800;}
.src-p:target {background: rgba(255, 120, 0, .38); border-left-color: #e05500;}
.quote {border-left: 3px solid #999; padding: 2px 10px; margin: 4px 0 8px 0; opacity: .9;}
.badge {display: inline-block; padding: 0 6px; border-radius: 4px; font-size: .8rem;}
.b-ok {background: rgba(0, 160, 80, .18);} .b-nd {background: rgba(130, 130, 130, .25);}
.b-rv {background: rgba(230, 60, 30, .22);}
</style>
"""

STATUS_BADGE = {ValueStatus.OK: ("b-ok", "подтверждено"), ValueStatus.NO_DATA: ("b-nd", "нет данных"),
                ValueStatus.REVIEW: ("b-rv", "требует проверки")}


# --- вспомогательные функции разметки --------------------------------------

def anchor(ev: Evidence, doc_index: dict[str, int]) -> str:
    return f"src-{doc_index[ev.filename]}-p{ev.paragraph}"


def ev_html(ev: Evidence, doc_index: dict[str, int]) -> str:
    return (f'<a href="#{anchor(ev, doc_index)}">📄 {html.escape(ev.label)}</a>'
            f'<div class="quote">{html.escape(ev.quote)}</div>')


def badge(status: ValueStatus) -> str:
    cls, text = STATUS_BADGE[status]
    return f'<span class="badge {cls}">{text}</span>'


def shown(value, status: ValueStatus) -> str:
    """None при «требует проверки» — значение не определено (а не отсутствует в документах)."""
    if value is None and status is ValueStatus.REVIEW:
        return "не определено"
    return fmt_value(value)


def cell(fv: FieldValue) -> str:
    text = fmt_value(fv.value)
    if fv.status is ValueStatus.REVIEW:
        return f"⚠ {text} — требует проверки" if fv.value is not None else "⚠ требует проверки"
    return text


def field_html(name: str, fv: FieldValue, proposals: list[Proposal], doc_index: dict[str, int]) -> str:
    parts = [f"<b>{FIELD_LABELS[name]}: {html.escape(shown(fv.value, fv.status))}</b> {badge(fv.status)}"]
    if fv.note:
        parts.append(f"<div><i>{html.escape(fv.note)}</i></div>")
    if fv.evidence:
        parts.append("<div>Основание:</div>" + "".join(ev_html(e, doc_index) for e in fv.evidence))
    elif fv.status is ValueStatus.NO_DATA:
        parts.append("<div>Основание: подходящий пункт в тексте не найден.</div>")
    if fv.changed:
        chain = " → ".join(
            f"{html.escape(shown(s.value, s.status))} ({s.source}: "
            + (", ".join(f'<a href="#{anchor(e, doc_index)}">{html.escape(e.label)}</a>' for e in s.evidence) or "—")
            + ")" for s in fv.history)
        parts.append(f"<div>История изменений: {chain}</div>")
    if fv.candidates:
        parts.append("<div>Кандидаты для ручной проверки:</div>" + "".join(
            f"<div>• {html.escape(fmt_value(v))}</div>{ev_html(e, doc_index)}" for v, e in fv.candidates))
    for p in proposals:
        extra = f", {p.note}" if p.note else ""
        parts.append(f"<div>⏳ <b>Не действует</b> ({p.kind.value}{html.escape(extra)}): "
                     f"{html.escape(fmt_value(p.value))}</div>{ev_html(p.evidence, doc_index)}")
    return "<div style='margin-bottom:14px'>" + "".join(parts) + "</div>"


def source_html(result: Result, doc_index: dict[str, int]) -> dict[str, str]:
    refs: set[str] = set()
    for row in result.rows:
        for f in ("supplier", "amount", "delivery"):
            fv = row.field(f)
            refs |= {anchor(e, doc_index) for e in fv.evidence + [e for _, e in fv.candidates]}
        refs |= {anchor(p.evidence, doc_index) for p in row.proposals}
    out = {}
    for doc in result.documents:
        lines = []
        for p in doc.paragraphs:
            aid = f"src-{doc_index[doc.filename]}-p{p.index}"
            cls = "src-p ref" if aid in refs else "src-p"
            lines.append(f'<div id="{aid}" class="{cls}"><span class="pn">{p.index}</span>'
                         f'{html.escape(p.text)}</div>')
        out[doc.filename] = "".join(lines) or "<i>файл пустой</i>"
    return out


# --- страница ----------------------------------------------------------------

st.set_page_config(page_title="Проверка договоров поставки", page_icon="📑", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
st.title("Проверка условий договоров поставки")
st.caption("Сумма и срок поставки по состоянию на дату проверки. У каждого значения указаны источник, "
           "цитата и ссылка на абзац исходного текста.")

check_date = st.date_input("Дата проверки", DEFAULT_CHECK_DATE, format="DD.MM.YYYY")
st.info(FORMAT_HINT, icon="ℹ️")
with st.expander("Правило учёта изменений", expanded=False):
    st.markdown("**Правило.** Условия меняет только дополнительное соглашение, подписанное обеими "
                "сторонами и вступившее в силу к дате проверки. Проекты, письма и ДС, которые ещё не "
                "вступили в силу, показываются отдельно. Имя файла и порядок загрузки на приоритет не влияют.")

uploaded = st.file_uploader("Загрузите TXT-файлы: договоры, доп. соглашения, письма, проекты",
                            type=["txt"], accept_multiple_files=True)
if not uploaded:
    st.info("Загрузите один или несколько TXT-файлов.")
    st.stop()

result = check([(f.name, f.getvalue()) for f in uploaded], check_date)
doc_index = {d.filename: i for i, d in enumerate(result.documents)}

# Предупреждения по документам
for d in result.documents:
    for w in d.warnings:
        st.warning(f"**{d.filename}**: {w}")

# 1. Сводная таблица
st.header(f"Договоры на {fmt_date(check_date)}")
if result.rows:
    table = pd.DataFrame([{
        "Договор": r.number,
        "Поставщик": cell(r.supplier),
        "Сумма, ₽ (с НДС)": cell(r.amount),
        "Срок поставки": cell(r.delivery),
        "Изменено документом": ", ".join(sorted({e.filename for f in ("amount", "delivery")
                                                  if r.field(f).changed for e in r.field(f).evidence})) or "—",
        "Несогласованные изменения": "; ".join(
            f"{FIELD_LABELS[p.field].lower()} → {fmt_value(p.value)} ({p.kind.value})" for p in r.proposals) or "—",
    } for r in result.rows])
    st.dataframe(table, hide_index=True, use_container_width=True)
else:
    st.info("Договоры не найдены среди загруженных файлов.")

# 2. Вопросы
st.header("Вопросы")
tab_field, tab_date = st.tabs(["Сумма или срок по договору", "Поставки раньше даты"])
with tab_field:
    numbers = [r.number for r in result.rows]
    if numbers:
        c1, c2 = st.columns(2)
        number = c1.selectbox("Договор", numbers)
        fname = c2.radio("Поле", ["delivery", "amount"], format_func=lambda f: FIELD_LABELS[f], horizontal=True)
        ans = field_answer(result, number, fname)
        verb = "действует" if fname == "delivery" else "составляет"
        st.markdown(f"**{FIELD_LABELS[fname]} по договору {number} на {fmt_date(check_date)} {verb}: "
                    f"{shown(ans.value.value, ans.value.status)}**")
        st.markdown(field_html(fname, ans.value, ans.proposals, doc_index), unsafe_allow_html=True)
with tab_date:
    before = st.date_input("Срок поставки раньше", date(2026, 4, 1), format="DD.MM.YYYY")
    q = deliveries_before(result, before)
    if q.matches:
        for row, props in q.matches:
            flag = (" — ⚠ есть несогласованные изменения срока: " +
                    "; ".join(f"{fmt_value(p.value)} ({p.kind.value}, {p.evidence.filename})" for p in props)
                    if props else " — несогласованных изменений срока нет")
            st.markdown(f"**{row.number}** ({row.supplier.value}): {fmt_value(row.delivery.value)}{flag}")
            st.markdown(field_html("delivery", row.delivery, props, doc_index), unsafe_allow_html=True)
    else:
        st.markdown(f"Договоров со сроком поставки раньше {fmt_date(before)} нет.")
    if q.review:
        st.warning("Срок не установлен надёжно, нужна ручная проверка: " + ", ".join(r.number for r in q.review))
    others = [p for p in q.all_pending if p.contract_number not in {r.number for r, _ in q.matches}]
    if others:
        st.markdown("Несогласованные изменения срока по другим договорам: " + "; ".join(
            f"{p.contract_number} → {fmt_value(p.value)} ({p.kind.value})" for p in others))

# 3. Основания
st.header("Основания по договорам")
for row in result.rows:
    with st.expander(f"{row.number} — {row.supplier.value or 'поставщик не указан'}"):
        for w in row.warnings:
            st.warning(w)
        st.markdown(field_html("supplier", row.supplier, [], doc_index), unsafe_allow_html=True)
        for f in ("amount", "delivery"):
            st.markdown(field_html(f, row.field(f), row.proposals_for(f), doc_index), unsafe_allow_html=True)
        st.caption("Связанные документы: " + "; ".join(
            f"{d.filename} ({d.doc_type.value}, {d.status.value})" for d in row.documents))

if result.orphans:
    st.header("Документы без договора")
    for o in result.orphans:
        if o.reason not in o.document.warnings:  # предупреждение уже показано выше
            st.warning(f"**{o.document.filename}**: {o.reason}")
        else:
            st.markdown(f"- **{o.document.filename}**: {o.reason}")

# 4. Загруженные документы и исходные тексты
st.header("Загруженные документы")
st.dataframe(pd.DataFrame([{
    "Файл": d.filename, "Тип": d.doc_type.value, "Договор": d.contract_number or "—",
    "Статус": d.status.value, "Вступает в силу": fmt_date(d.effective_date),
    "Замечания": "; ".join(d.warnings) or "—",
} for d in result.documents]), hide_index=True, use_container_width=True)

st.subheader("Исходные тексты")
st.caption("Абзацы, на которые ссылаются значения, подсвечены. Ссылка 📄 прокручивает к нужному абзацу.")
for name, body in source_html(result, doc_index).items():
    st.markdown(f"**{html.escape(name)}**", unsafe_allow_html=True)
    st.markdown(body, unsafe_allow_html=True)
