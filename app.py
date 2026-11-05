"""Streamlit-интерфейс для закупщика. Логика извлечения — в пакете checker/."""
from __future__ import annotations

import html
from datetime import date

import streamlit as st

from checker.models import Evidence, FieldValue, Proposal, Result, ValueStatus
from checker.queries import FIELD_LABELS, deliveries_before, field_answer, fmt_value, group_documents
from checker.resolver import check, fmt_date

DEFAULT_CHECK_DATE = date(2026, 3, 10)

FORMAT_HINT = """
**Как распознаются документы.** Прототип работает на правилах, без LLM, и ищет признаки по всему тексту, а не по позиции строки:
- **тип** — по заголовку в начале документа («договор поставки», «дополнительное соглашение», «проект … соглашения», «письмо»); письмо без заголовка — по признакам («Уважаемые», «просим», «От:/Кому:»);
- **номер договора** — после «договор №», «к договору», «по договору», в любом формате (`STN-101`, `45/2026`);
- **статус** — строка «Статус:» или фраза в тексте («подписан(о) обеими сторонами», «проект», «не подписан»); подпись одной стороной или противоречивые признаки → «требует проверки»;
- **вступление ДС в силу** — «вступает в силу ДД.ММ.ГГГГ» или «с даты подписания»;
- **даты** — `ДД.ММ.ГГГГ` или «15 апреля 2026 г.»; **суммы** — цифрами с «руб.», «рублей», «₽» или «RUB».

Ограничения: только TXT (UTF-8 или cp1251); срок «в течение N дней» не пересчитывается в дату; сумма в иностранной валюте и цена в спецификации не извлекаются. Если признак не найден, значение не подставляется: документ получает предупреждение или статус «требует проверки».
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
table.grp {width: 100%; border-collapse: collapse; font-size: .9rem;}
table.grp th, table.grp td {padding: 5px 8px; border-bottom: 1px solid rgba(128, 128, 128, .25);
                            text-align: left; vertical-align: top;}
table.grp th {font-weight: 600; opacity: .8;}
table.grp tr.g-contract td {background: rgba(70, 130, 220, .14); font-weight: 600;
                            border-top: 2px solid rgba(70, 130, 220, .55);}
table.grp tr.g-orphan td {background: rgba(230, 60, 30, .12); font-weight: 600;
                          border-top: 2px solid rgba(230, 60, 30, .5);}
table.grp tr.g-doc td {font-weight: 400;}
details.src-grp {border: 1px solid rgba(70, 130, 220, .45); border-radius: 6px; margin: 10px 0; padding: 4px 10px;}
details.src-grp.orphan {border-color: rgba(230, 60, 30, .5);}
details.src-grp > summary {font-weight: 600; font-size: 1.05rem; cursor: pointer; padding: 4px 0;}
details.src-doc {margin: 6px 0 6px 14px;}
details.src-doc > summary {cursor: pointer; padding: 2px 0;}
details.src-doc > summary .meta {opacity: .65; font-weight: 400;}
table.grp td:first-child, table.grp td:nth-child(5), table.grp td:nth-child(6),
table.grp td:nth-child(4) {white-space: nowrap;}
table.grp tr.g-doc td:first-child {padding-left: 28px;}
table.grp tr.g-doc td:first-child::before {content: "└ "; opacity: .5;}
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
            refs |= {anchor(e, doc_index) for e in fv.evidence + [e for _, e in fv.candidates]
                     + [e for step in fv.history for e in step.evidence]}
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


GROUP_COLUMNS = ["Договор / документ", "Поставщик / тип документа", "Статус документа", "Вступает в силу",
                 "Сумма, ₽ (с НДС)", "Срок поставки", "Изменено документом",
                 "Несогласованные изменения / замечания"]


def grouped_table_html(result: Result, doc_index: dict[str, int]) -> str:
    """Одна таблица: строка договора с итоговыми значениями, под ней — относящиеся к нему файлы."""
    esc = html.escape
    orphan_reason = {o.document.filename: o.reason for o in result.orphans}

    def tr(cls: str, cells: list[str]) -> str:
        return f'<tr class="{cls}">' + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

    rows = ["<table class='grp'><thead><tr>" + "".join(f"<th>{c}</th>" for c in GROUP_COLUMNS)
            + "</tr></thead><tbody>"]
    for row, docs in group_documents(result):
        if row is not None:
            changed_by = sorted({e.filename for f in ("amount", "delivery")
                                 if row.field(f).changed for e in row.field(f).evidence})
            pending = "; ".join(f"{FIELD_LABELS[p.field].lower()} → {fmt_value(p.value)} ({p.kind.value})"
                                for p in row.proposals)
            rows.append(tr("g-contract", [
                esc(row.number), esc(cell(row.supplier)), "", "", esc(cell(row.amount)),
                esc(cell(row.delivery)), esc(", ".join(changed_by) or "—"), esc(pending or "—")]))
        else:
            rows.append(tr("g-orphan", ["Не привязаны к договору", "", "", "", "", "", "", ""]))
        for d in docs:
            notes = [orphan_reason[d.filename]] if d.filename in orphan_reason else []
            notes += [w for w in d.warnings if w not in notes]
            link = f'<a href="#src-{doc_index[d.filename]}-p1">{esc(d.filename)}</a>' if d.paragraphs else esc(d.filename)
            rows.append(tr("g-doc", [
                link, esc(d.doc_type.value), esc(d.status.value), esc(fmt_date(d.effective_date)),
                "", "", "", esc("; ".join(notes) or "—")]))
    rows.append("</tbody></table>")
    return "".join(rows)


def sources_grouped_html(result: Result, doc_index: dict[str, int]) -> str:
    """Исходные тексты, сгруппированные как таблица: договор → его файлы, в конце — не привязанные.
    Группы — <details open>: их можно свернуть; переход по якорю на абзац раскрывает блок."""
    bodies = source_html(result, doc_index)
    out = []
    for row, docs in group_documents(result):
        if row is not None:
            title, cls = f"{row.number} — {row.supplier.value or 'поставщик не указан'}", "src-grp"
        else:
            title, cls = "Не привязаны к договору", "src-grp orphan"
        inner = "".join(
            f'<details open class="src-doc"><summary>📄 <b>{html.escape(d.filename)}</b> '
            f'<span class="meta">· {html.escape(d.doc_type.value)} · {html.escape(d.status.value)}</span>'
            f'</summary>{bodies[d.filename]}</details>' for d in docs)
        out.append(f'<details open class="{cls}"><summary>{html.escape(title)}</summary>{inner}</details>')
    return "".join(out)


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

# 1. Договоры и относящиеся к ним документы
st.header(f"Договоры на {fmt_date(check_date)}")
st.caption("Строка договора — итоговые значения на дату проверки. Под ней — загруженные файлы, которые к нему "
           "относятся (привязка по номеру договора). Имя файла ведёт к его тексту.")
if not result.rows:
    st.info("Договоры не найдены среди загруженных файлов.")
st.markdown(grouped_table_html(result, doc_index), unsafe_allow_html=True)

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

# 4. Исходные тексты
st.header("Исходные тексты")
st.caption("Сгруппированы по договорам, как в таблице. Абзацы, на которые ссылаются значения, подсвечены; "
           "ссылка 📄 прокручивает к нужному абзацу. Группы и файлы можно свернуть кликом по заголовку.")
st.markdown(sources_grouped_html(result, doc_index), unsafe_allow_html=True)
