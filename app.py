"""Streamlit-интерфейс для закупщика. Логика извлечения — в пакете checker/.

Макет: настройки и загрузка — в боковой панели; основной экран начинается с результатов:
сводка → быстрый ответ → «требует внимания» → таблица договоров → карточка договора
(значения слева, исходные тексты справа, переход к абзацу без ухода со страницы).
"""
from __future__ import annotations

import html
from datetime import date, timedelta

import streamlit as st
import streamlit.components.v1 as components

from checker.models import (Document, Evidence, FieldValue, Proposal, Result, SignStatus,
                            ValueStatus)
from checker.queries import (FIELD_LABELS, deliveries_before, doc_label, field_answer, fmt_value,
                             group_documents)
from checker.resolver import check, fmt_date

DEFAULT_CHECK_DATE = date(2026, 3, 10)
ORPHANS = "Не привязаны к договору"

FORMAT_HINT = """
Прототип работает на правилах, без LLM, и ищет признаки по всему тексту:
- **тип** — по заголовку («договор поставки», «дополнительное соглашение», «проект … соглашения», «письмо»); письмо без заголовка — по признакам («Уважаемые», «просим», «От:/Кому:»);
- **номер договора** — после «договор №», «к договору», «по договору», в любом формате (`STN-101`, `45/2026`);
- **статус** — строка «Статус:» или фраза в тексте («подписан(о) обеими сторонами», «проект», «не подписан»);
- **вступление ДС в силу** — «вступает в силу ДД.ММ.ГГГГ» или «с даты подписания»;
- **даты** — `ДД.ММ.ГГГГ` или «15 апреля 2026 г.»; **суммы** — цифрами с «руб.», «₽» или «RUB».

Не распознаётся: срок «в течение N дней» (→ «требует проверки»), валюта, цена в спецификации, PDF/DOCX.
"""
RULE = ("Условия меняет только дополнительное соглашение, **подписанное обеими сторонами и вступившее "
        "в силу** к дате проверки. Проекты, письма и ещё не вступившие ДС показываются как «не действует». "
        "Имя файла и порядок загрузки на приоритет не влияют.")

# --- единая система статусов --------------------------------------------------

STATUS = {
    ValueStatus.OK: ("✅", "подтверждено", "s-ok"),
    ValueStatus.REVIEW: ("⚠️", "требует проверки", "s-rv"),
    ValueStatus.NO_DATA: ("∅", "нет данных", "s-nd"),
}
PENDING = ("⏳", "не действует", "s-pd")
DOC_STATUS = {
    SignStatus.SIGNED: ("✅", "подписан", "s-ok"),
    SignStatus.DRAFT: ("⏳", "проект", "s-pd"),
    SignStatus.PROPOSAL: ("⏳", "предложение", "s-pd"),
    SignStatus.UNKNOWN: ("⚠️", "статус не распознан", "s-rv"),
}

CSS = """
<style>
.badge {display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: .78rem; font-weight: 500;
        vertical-align: middle; white-space: nowrap;}
.s-ok {background: rgba(0, 160, 80, .16);} .s-rv {background: rgba(230, 60, 30, .18);}
.s-nd {background: rgba(130, 130, 130, .22);} .s-pd {background: rgba(230, 170, 0, .22);}
.muted {opacity: .65;} .small {font-size: .82rem;}
.ans-q {opacity: .7; font-size: .9rem;}
.ans-v {font-size: 2rem; font-weight: 700; line-height: 1.2; margin: 2px 0 6px 0;}
.fld {margin-bottom: 20px;}
.fld-h {opacity: .65; font-size: .8rem; text-transform: uppercase; letter-spacing: .03em;}
.fld-v {font-size: 1.35rem; font-weight: 650; margin: 2px 0 4px 0;}
.quote {border-left: 3px solid rgba(128, 128, 128, .6); padding: 2px 10px; margin: 3px 0 8px 0; font-size: .9rem;
        white-space: pre-wrap;}
.pend {border-left: 3px solid rgba(230, 170, 0, .8); padding: 2px 10px; margin: 6px 0;}
.src-p {padding: 2px 8px; border-left: 3px solid transparent; white-space: pre-wrap; font-size: .86rem;
        font-family: ui-monospace, monospace; scroll-margin-top: 20px;}
.src-p .pn {opacity: .5; margin-right: .6em; user-select: none;}
.src-p.ref {background: rgba(255, 200, 0, .15); border-left-color: #e0a800;}
.src-p.focus, .src-p:target {background: rgba(255, 120, 0, .36); border-left-color: #e05500;}
details.src-doc {margin: 4px 0 10px 0;}
details.src-doc > summary {cursor: pointer; font-weight: 600; padding: 3px 0;}
.legend div {margin: 3px 0;}
</style>
"""


def badge(icon: str, text: str, cls: str) -> str:
    return f'<span class="badge {cls}">{icon} {html.escape(text)}</span>'


def value_badge(status: ValueStatus) -> str:
    return badge(*STATUS[status])


def value_with_badge(value, status: ValueStatus) -> str:
    """Значение и бейдж статуса; для «нет данных» бейдж не повторяет текст значения."""
    text = html.escape(shown(value, status))
    if status is ValueStatus.NO_DATA:
        return f'<span class="muted">∅ {text}</span>'
    return f"{text} {value_badge(status)}"


def shown(value, status: ValueStatus) -> str:
    """None при «требует проверки» — значение не определено (а не отсутствует в документах)."""
    if value is None and status is ValueStatus.REVIEW:
        return "не определено"
    return fmt_value(value)


def cell(fv: FieldValue) -> str:
    return f"{STATUS[fv.status][0]} {shown(fv.value, fv.status)}"


# --- контекст отрисовки -------------------------------------------------------

class View:
    """Документы по имени файла, якоря абзацев и множество абзацев-оснований для подсветки."""

    def __init__(self, result: Result):
        self.result = result
        self.docs = {d.filename: d for d in result.documents}
        self.index = {d.filename: i for i, d in enumerate(result.documents)}
        self.refs: set[str] = set()
        for row in result.rows:
            for f in ("supplier", "amount", "delivery"):
                fv = row.field(f)
                for e in fv.evidence + [e for _, e in fv.candidates] + [e for s in fv.history for e in s.evidence]:
                    self.refs.add(self.anchor(e))
            self.refs |= {self.anchor(p.evidence) for p in row.proposals}

    def anchor(self, ev: Evidence) -> str:
        return f"src-{self.index[ev.filename]}-p{ev.paragraph}"

    def ev_label(self, ev: Evidence) -> str:
        where = f"п. {ev.clause}" if ev.clause else f"абзац {ev.paragraph}"
        return f"{doc_label(self.docs[ev.filename])}, {where}"

    def ev_html(self, ev: Evidence, link: bool = True) -> str:
        label = html.escape(self.ev_label(ev))
        head = f'<a href="#{self.anchor(ev)}">📄 {label}</a>' if link else f"📄 {label}"
        return (f'{head} <span class="muted small">{html.escape(ev.filename)}</span>'
                f'<div class="quote">{html.escape(ev.quote)}</div>')

    def excerpt_html(self, ev: Evidence, around: int = 2) -> str:
        """Абзац-основание в контексте соседних абзацев — для проверки прямо в ответе."""
        doc = self.docs[ev.filename]
        lo, hi = max(1, ev.paragraph - around), min(len(doc.paragraphs), ev.paragraph + around)
        rows = []
        for p in doc.paragraphs[lo - 1:hi]:
            cls = "src-p focus" if p.index == ev.paragraph else "src-p"
            rows.append(f'<div class="{cls}"><span class="pn">{p.index}</span>{html.escape(p.text)}</div>')
        return f'<div class="small muted">{html.escape(doc_label(doc))} · {html.escape(doc.filename)}</div>' + "".join(rows)

    def proposal_html(self, p: Proposal, link: bool = True) -> str:
        extra = f", {p.note}" if p.note else ""
        return (f'<div class="pend">{badge(*PENDING)} <b>{html.escape(fmt_value(p.value))}</b> '
                f'<span class="muted">— {html.escape(p.kind.value + extra)}</span><br>{self.ev_html(p.evidence, link)}</div>')

    def field_html(self, name: str, fv: FieldValue, proposals: list[Proposal]) -> str:
        parts = [f'<div class="fld"><div class="fld-h">{FIELD_LABELS[name]}</div>'
                 f'<div class="fld-v">{value_with_badge(fv.value, fv.status)}</div>']
        if fv.note:
            parts.append(f'<div class="small"><i>{html.escape(fv.note)}</i></div>')
        if fv.evidence:
            parts.append("".join(self.ev_html(e) for e in fv.evidence))
        elif fv.status is ValueStatus.NO_DATA:
            parts.append('<div class="small muted">Подходящий пункт в тексте не найден.</div>')
        if fv.changed:
            chain = " → ".join(
                f"<b>{html.escape(shown(s.value, s.status))}</b> ("
                + ", ".join(f'<a href="#{self.anchor(e)}">{html.escape(self.ev_label(e))}</a>' for e in s.evidence)
                + f"{'; ' + html.escape(s.source) if s.source != 'договор' else ''})" for s in fv.history)
            parts.append(f'<div class="small">История: {chain}</div>')
        if fv.candidates:
            parts.append('<div class="small">Кандидаты для ручной проверки:</div>' + "".join(
                f"<div>• <b>{html.escape(fmt_value(v))}</b></div>{self.ev_html(e)}" for v, e in fv.candidates))
        parts += [self.proposal_html(p) for p in proposals]
        return "".join(parts) + "</div>"

    def sources_html(self, docs: list[Document], focus: str | None) -> str:
        out = []
        for d in docs:
            body = []
            for p in d.paragraphs:
                aid = f"src-{self.index[d.filename]}-p{p.index}"
                cls = "src-p" + (" ref" if aid in self.refs else "") + (" focus" if aid == focus else "")
                body.append(f'<div id="{aid}" class="{cls}"><span class="pn">{p.index}</span>{html.escape(p.text)}</div>')
            out.append(f'<details open class="src-doc"><summary>{html.escape(doc_label(d))} '
                       f'{badge(*DOC_STATUS[d.status])} <span class="muted small">{html.escape(d.filename)}</span>'
                       f'</summary>{"".join(body) or "<i>файл пустой</i>"}</details>')
        return "".join(out)


def doc_name(d: Document) -> str:
    """«ДС № 1 от 01.03.2026 (02_amendment_signed.txt)»; имя файла не повторяется, если оно и есть название."""
    label = doc_label(d)
    return label if label == d.filename else f"{label} ({d.filename})"


def open_card(target: str, focus: str | None = None) -> None:
    st.session_state.card = target
    st.session_state.focus = focus
    st.session_state.scroll = True


# --- боковая панель -------------------------------------------------------------

st.set_page_config(page_title="Проверка договоров поставки", page_icon="📑", layout="wide",
                   initial_sidebar_state="expanded")
st.markdown(CSS, unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### 📑 Проверка договоров поставки")
    check_date = st.date_input("Дата проверки", DEFAULT_CHECK_DATE, format="DD.MM.YYYY")
    uploaded = st.file_uploader("Документы (TXT)", type=["txt"], accept_multiple_files=True,
                                help="Договоры, доп. соглашения, проекты и письма. Порядок и имена файлов не важны.")
    st.markdown('<div class="legend small"><b>Статусы</b>'
                f'<div>{badge(*STATUS[ValueStatus.OK])} значение из действующих документов</div>'
                f'<div>{badge(*STATUS[ValueStatus.REVIEW])} надёжно определить нельзя</div>'
                f'<div>{badge(*STATUS[ValueStatus.NO_DATA])} в документах значения нет</div>'
                f'<div>{badge(*PENDING)} проект, письмо или ДС не в силе</div></div>',
                unsafe_allow_html=True)
    with st.expander("Правило учёта изменений"):
        st.markdown(RULE)
    with st.expander("Как распознаются документы"):
        st.markdown(FORMAT_HINT)

if not uploaded:
    st.title("Условия договоров поставки")
    st.info("Загрузите TXT-файлы в панели слева: договоры, дополнительные соглашения, проекты и письма поставщиков.")
    c1, c2, c3 = st.columns(3)
    c1.markdown("**1. Сводка**  \nСумма и срок поставки по каждому договору на дату проверки.")
    c2.markdown("**2. Ответ на вопрос**  \nСрок или сумма по договору, поставки раньше даты.")
    c3.markdown("**3. Проверка**  \nКаждое значение — с цитатой и абзацем исходного документа.")
    st.stop()

result = check([(f.name, f.getvalue()) for f in uploaded], check_date)
view = View(result)
rows = {r.number: r for r in result.rows}
numbers = list(rows)
docs_of = {r.number: d for r, d in group_documents(result) if r is not None}  # документы договора в порядке показа

# --- 1. сводка ----------------------------------------------------------------------

st.title(f"Условия договоров на {fmt_date(check_date)}")
fields = [row.field(f) for row in result.rows for f in ("amount", "delivery")]
count = lambda s: sum(fv.status is s for fv in fields)
k = st.columns(6)
k[0].metric("Договоров", len(result.rows))
k[1].metric("✅ Подтверждено", f"{count(ValueStatus.OK)} из {len(fields)}",
            help="Значений суммы и срока, подтверждённых действующими документами")
k[2].metric("⚠️ Требует проверки", count(ValueStatus.REVIEW))
k[3].metric("∅ Нет данных", count(ValueStatus.NO_DATA))
k[4].metric("⏳ Не действует", sum(len(r.proposals) for r in result.rows),
            help="Изменения из проектов, писем и ещё не вступивших ДС")
k[5].metric("Без договора", len(result.orphans), help="Файлы, которые не удалось привязать к договору")

# --- 2. быстрый ответ ---------------------------------------------------------------

st.subheader("Быстрый ответ")
QUESTIONS = {"Срок поставки": "delivery", "Сумма договора": "amount", "Поставки раньше даты": "before"}
qtype = st.pills("Вопрос", list(QUESTIONS), default="Срок поставки", key="qtype")
if qtype and QUESTIONS[qtype] in ("delivery", "amount") and numbers:
    fname = QUESTIONS[qtype]
    number = st.pills("Договор", numbers, default=numbers[0], key="q_contract_" + "|".join(numbers)) or numbers[0]
    ans = field_answer(result, number, fname)
    fv = ans.value
    with st.container(border=True):
        what = "Срок поставки" if fname == "delivery" else "Сумма"
        st.markdown(f'<div class="ans-q">{what} по договору {html.escape(number)} на {fmt_date(check_date)}</div>'
                    f'<div class="ans-v">{value_with_badge(fv.value, fv.status)}</div>', unsafe_allow_html=True)
        if fv.changed and fv.status is ValueStatus.OK:
            last = fv.history[-1]
            why = (f"Изменено: {view.ev_label(last.evidence[0])} ({last.source.removeprefix('ДС, ')}). "
                   f"По договору было {shown(fv.history[0].value, fv.history[0].status)}.")
        elif fv.status is ValueStatus.OK:
            why = f"По тексту: {view.ev_label(fv.evidence[0])}."
        else:
            why = fv.note
        st.markdown(f"<div>{html.escape(why)}</div>", unsafe_allow_html=True)
        if fv.evidence:
            st.markdown(view.ev_html(fv.evidence[0], link=False), unsafe_allow_html=True)
        for p in ans.proposals:
            st.markdown(view.proposal_html(p, link=False), unsafe_allow_html=True)
        b1, b2 = st.columns([1, 3])
        focus = view.anchor(fv.evidence[0]) if fv.evidence else None
        b1.button("Открыть в документе", key="ans_open", on_click=open_card, args=(number, focus),
                  type="primary", use_container_width=True)
        if fv.evidence:
            with b2.expander("Показать абзац в контексте"):
                st.markdown(view.excerpt_html(fv.evidence[0]), unsafe_allow_html=True)
elif qtype and QUESTIONS[qtype] == "before":
    nxt = (check_date.replace(day=1) + timedelta(days=32)).replace(day=1)  # 1-е число следующего месяца
    before = st.date_input("Срок поставки раньше", nxt, format="DD.MM.YYYY")
    q = deliveries_before(result, before)
    with st.container(border=True):
        st.markdown(f'<div class="ans-q">Поставка раньше {fmt_date(before)}</div>'
                    f'<div class="ans-v">{len(q.matches)} {"договор" if len(q.matches) == 1 else "договора(ов)"}</div>',
                    unsafe_allow_html=True)
        for row, props in q.matches:
            c1, c2 = st.columns([5, 1])
            flag = ("".join(view.proposal_html(p, link=False) for p in props) if props
                    else '<div class="small muted">Несогласованных изменений срока нет.</div>')
            c1.markdown(f"<b>{html.escape(row.number)}</b> · {html.escape(row.supplier.value or '')} · "
                        f"<b>{fmt_value(row.delivery.value)}</b> {value_badge(row.delivery.status)}{flag}",
                        unsafe_allow_html=True)
            c2.button("Открыть", key=f"q4_{row.number}", on_click=open_card,
                      args=(row.number, view.anchor(row.delivery.evidence[0])))
        if q.review:
            st.markdown(badge(*STATUS[ValueStatus.REVIEW]) + " срок не установлен надёжно: "
                        + ", ".join(html.escape(r.number) for r in q.review), unsafe_allow_html=True)
        matched = {r.number for r, _ in q.matches}
        others = [p for p in q.all_pending if p.contract_number not in matched]
        if others:
            st.caption("Несогласованные изменения срока по другим договорам: " + "; ".join(
                f"{p.contract_number} → {fmt_value(p.value)} ({p.kind.value})" for p in others))

# --- 3. требует внимания --------------------------------------------------------------

attention: list[tuple[str, str, str | None]] = []   # (текст, куда открыть, якорь)
for row in result.rows:
    for f in ("amount", "delivery"):
        fv = row.field(f)
        if fv.status is not ValueStatus.OK:
            icon, word, _ = STATUS[fv.status]
            attention.append((f"{icon} **{row.number}** · {FIELD_LABELS[f].lower()} — {word}: {fv.note}",
                              row.number, view.anchor(fv.evidence[0]) if fv.evidence else None))
    attention += [(f"⚠️ **{row.number}** · {w}", row.number, None) for w in row.warnings]
orphan_names = {o.document.filename for o in result.orphans}
for o in result.orphans:
    attention.append((f"⚠️ **{doc_name(o.document)}** — {o.reason}", ORPHANS, None))
for d in result.documents:
    if d.filename not in orphan_names:
        attention += [(f"ℹ️ **{doc_name(d)}** — {w}", d.contract_number, None) for w in d.warnings]
if attention:
    st.subheader("Требует внимания")
    with st.container(border=True):
        for i, (text, target, focus) in enumerate(attention):
            c1, c2 = st.columns([6, 1])
            c1.markdown(text)
            if target:
                c2.button("Открыть", key=f"att_{i}", on_click=open_card, args=(target, focus))

# --- 4. договоры ----------------------------------------------------------------------

st.subheader("Договоры")
if result.rows:
    table = [{
        "Договор": r.number,
        "Поставщик": r.supplier.value or "—",
        "Сумма": cell(r.amount),
        "Срок поставки": cell(r.delivery),
        "Изменено": ", ".join(sorted({doc_label(view.docs[e.filename]) for f in ("amount", "delivery")
                                      if r.field(f).changed for e in r.field(f).evidence})) or "—",
        "Не действует": f"⏳ {len(r.proposals)}" if r.proposals else "—",
        "Документы": " · ".join(f"{DOC_STATUS[d.status][0]} {doc_label(d)}" for d in docs_of[r.number]),
    } for r in result.rows]
    event = st.dataframe(table, hide_index=True, use_container_width=True, on_select="rerun",
                         selection_mode="single-row", key="contracts_table",
                         column_config={"Документы": st.column_config.TextColumn(width="large")})
    st.caption("Выберите строку, чтобы открыть карточку договора с основаниями и исходными текстами.")
    sel = event.selection.rows
    if sel and st.session_state.get("table_sel") != sel:
        st.session_state.table_sel = sel
        open_card(result.rows[sel[0]].number)
else:
    st.info("Договоры среди загруженных файлов не найдены.")

# --- 5. карточка договора ----------------------------------------------------------

options = numbers + ([ORPHANS] if result.orphans else [])
if options:
    st.markdown('<div id="card"></div>', unsafe_allow_html=True)
    st.subheader("Карточка договора")
    current = st.session_state.get("card")
    target = st.selectbox("Договор", options, index=options.index(current) if current in options else 0,
                          format_func=lambda n: n if n == ORPHANS else f"{n} — {rows[n].supplier.value or ''}",
                          label_visibility="collapsed")
    if target != current:
        st.session_state.card, st.session_state.focus = target, None
    focus = st.session_state.get("focus")
    left, right = st.columns([1, 1], gap="large")
    if target == ORPHANS:
        docs = [o.document for o in result.orphans]
        with left:
            for o in result.orphans:
                st.markdown(f"⚠️ **{doc_name(o.document)}** — {o.reason}")
    else:
        row = rows[target]
        docs = docs_of[target]
        with left:
            for w in row.warnings:
                st.warning(w)
            for f in ("supplier", "amount", "delivery"):
                proposals = row.proposals_for(f) if f != "supplier" else []
                st.markdown(view.field_html(f, row.field(f), proposals), unsafe_allow_html=True)
    with right:
        st.markdown('<div class="small muted">Документы договора. Подсвечены абзацы-основания; '
                    'ссылка 📄 слева прокручивает эту панель.</div>', unsafe_allow_html=True)
        with st.container(height=620):
            st.markdown(view.sources_html(docs, focus), unsafe_allow_html=True)

    # Ссылки 📄 прокручивают только панель документов: стандартный переход по якорю
    # сдвигал бы и всю страницу. Обработчик ставится один раз на страницу.
    do_scroll = st.session_state.pop("scroll", False)       # карточку только что открыли кнопкой или из таблицы
    scroll_to = f'"{focus}"' if do_scroll and focus else "null"
    open_card_now = "true" if do_scroll else "false"
    components.html(f"""<script>
      const w = window.parent, d = w.document;
      w.__scrollToSrc = (id) => {{
        const t = d.getElementById(id); if (!t) return false;
        const det = t.closest("details"); if (det) det.open = true;   // сначала раскрыть документ
        // Ближайшая панель с собственной прокруткой; саму страницу (stMain) не прокручиваем никогда.
        const main = d.querySelector('[data-testid="stMain"]');
        let box = t.parentElement;
        while (box && box !== main && !/(auto|scroll|overlay)/.test(getComputedStyle(box).overflowY))
          box = box.parentElement;
        if (box === main) box = null;
        d.querySelectorAll(".src-p.focus").forEach(e => e.classList.remove("focus"));
        t.classList.add("focus");
        if (box) {{
          // Центр видимой на экране части панели, а не всей панели: панель может быть выше окна.
          const r = box.getBoundingClientRect();
          const visTop = Math.max(r.top, 0), visBottom = Math.min(r.bottom, w.innerHeight);
          const delta = t.getBoundingClientRect().top + t.offsetHeight / 2 - (visTop + visBottom) / 2;
          box.scrollBy({{top: delta, behavior: "smooth"}});
        }}
        return true;
      }};
      if (!w.__srcLinksBound) {{
        w.__srcLinksBound = true;
        d.addEventListener("click", (e) => {{
          const a = e.target.closest && e.target.closest('a[href^="#src-"]');
          if (!a) return;
          if (w.__scrollToSrc(a.getAttribute("href").slice(1))) e.preventDefault();
        }}, true);
      }}
      if ({open_card_now}) setTimeout(() => {{
        const card = d.getElementById("card"); if (card) card.scrollIntoView({{behavior: "smooth"}});
        const id = {scroll_to}; if (id) setTimeout(() => w.__scrollToSrc(id), 500);
      }}, 250);
    </script>""", height=0)
