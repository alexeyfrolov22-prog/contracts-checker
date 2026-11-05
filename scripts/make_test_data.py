"""Генерирует test_data/ из documents/ — наборы для ручных тест-кейсов и автотестов.

Каждая мутация — точечная замена строки; если исходная строка не найдена, скрипт падает,
чтобы набор не оказался молча неизменённым.

Запуск: python scripts/make_test_data.py
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "documents"
OUT = ROOT / "test_data"

BASE = sorted(p.name for p in SRC.glob("*.txt"))


def read(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def sub(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"фрагмент не найден однозначно: {old!r}"
    return text.replace(old, new)


def drop_line(text: str, startswith: str) -> str:
    lines = text.split("\n")
    kept = [l for l in lines if not l.startswith(startswith)]
    assert len(kept) == len(lines) - 1, f"строка не найдена: {startswith!r}"
    return "\n".join(kept)


AMENDMENT_TPL = """ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ № {no} К ДОГОВОРУ STN-101
Дата: {date}
Статус: подписано обеими сторонами {date}.
Покупатель: ООО «СеверМет». Поставщик: ООО «Вектор Прибор».

1. Пункт 3.1 договора изложить в следующей редакции: «Поставщик обязуется поставить оборудование не позднее {term}».

2. Остальные условия договора с учётом дополнительного соглашения № 1 остаются без изменений.

3. Настоящее соглашение вступает в силу {date}.
"""

NEW_CONTRACT = """ДОГОВОР ПОСТАВКИ № STN-104
Дата: 25.02.2026
Статус: подписан обеими сторонами 25.02.2026.

1.1. Покупатель: ООО «СеверМет». Поставщик: ООО «Альфа Ресурс».
1.2. Предмет: поставка расходных материалов.

2.1. Общая стоимость поставки составляет 450 000 (четыреста пятьдесят тысяч) рублей, включая НДС. Указанная сумма — итоговая стоимость договора.
2.2. Авансовый платеж составляет 135 000 рублей и входит в общую стоимость по пункту 2.1.

3.1. Поставщик обязуется поставить расходные материалы не позднее 25.03.2026.
3.2. Оплата оставшейся суммы производится не позднее 15.04.2026. Этот срок является сроком оплаты, а не поставки.

4.1. Неустойка за просрочку поставки составляет 0,1% стоимости просроченной части поставки за каждый день, но не более 10% стоимости этой части.

5.1. Изменения условий действуют после подписания дополнительного соглашения обеими сторонами и наступления указанной в нем даты вступления в силу.
"""


# Варианты формулировок того же смысла (проверка распознавания по всему тексту, TC-25…TC-37).
_C = ("1.1. Покупатель: ООО «СеверМет». Поставщик: ООО «Бета».\n"
      "2.1. Общая стоимость поставки составляет 500 000 рублей, включая НДС.\n"
      "3.1. Поставщик обязуется поставить товар не позднее 15.04.2026.\n")
_SIGNED = "Статус: подписан обеими сторонами 05.02.2026.\n\n"


def _contract(no: str, body: str = _C, head: str = "") -> str:
    return f"{head}ДОГОВОР ПОСТАВКИ № {no}\nДата: 05.02.2026\n{_SIGNED}{body}"


ROBUST = {
    "v01_title_not_first.txt": _contract("STN-201", head="ООО «СеверМет»\nг. Москва\n\n"),
    "v02_no_status_line.txt": ("ДОГОВОР ПОСТАВКИ № STN-202\nДата: 05.02.2026\n\n" + _C
                               + "\nДоговор подписан обеими сторонами 05.02.2026.\n"),
    "v03_slash_number.txt": _contract("45/2026"),
    "v04_price_synonym.txt": _contract("STN-204", "1.1. Поставщик: ООО «Бета».\n"
                                       "2.1. Цена Договора составляет 500 000,00 руб.\n"
                                       "3.1. Срок поставки: до 15.04.2026.\n"),
    "v05_sum_rub_word_date.txt": _contract("STN-205", "1.1. Поставщик: ООО «Бета».\n"
                                           "2.1. Сумма договора — 500 000 RUB с НДС.\n"
                                           "3.1. Поставка осуществляется в срок до 15 апреля 2026 г.\n"),
    "v06_line_wraps.txt": _contract("STN-206", "1.1. Поставщик: ООО «Бета».\n"
                                    "2.1. Общая стоимость поставки составляет\n500 000 рублей, включая НДС.\n"
                                    "3.1. Поставщик обязуется поставить товар не\nпозднее 15.04.2026.\n"),
    "v07_relative_term.txt": _contract("STN-207", "1.1. Поставщик: ООО «Бета».\n"
                                       "2.1. Общая стоимость поставки составляет 500 000 рублей.\n"
                                       "3.1. Поставщик обязуется поставить товар в течение 30 дней "
                                       "с даты подписания.\n"),
    "v08_contract.txt": _contract("STN-208"),
    "v08_amendment_on_signing.txt": ("ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ № 1 К ДОГОВОРУ STN-208\n"
                                     "Статус: подписано обеими сторонами 01.03.2026.\n\n"
                                     "1. Пункт 3.1 изложить в следующей редакции: «Поставщик обязуется "
                                     "поставить товар не позднее 30.04.2026».\n"
                                     "2. Соглашение вступает в силу с даты его подписания.\n"),
    "v09_contract.txt": _contract("STN-209"),
    "v09_amendment_extended.txt": ("ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ № 1 К ДОГОВОРУ № STN-209\n"
                                   "Статус: подписано обеими сторонами 01.03.2026.\n\n"
                                   "1. Срок поставки по договору продлевается до 30.04.2026.\n"
                                   "2. Соглашение вступает в силу 01.03.2026.\n"),
    "v10_contract.txt": _contract("STN-210"),
    "v10_letter_no_title.txt": ("Уважаемые коллеги!\nВ связи с задержкой просим перенести срок поставки "
                                "по договору № STN-210 на 25.04.2026.\nС уважением, ООО «Бета»\n"),
    "v10b_contract.txt": _contract("STN-214"),
    "v10b_letter_number_in_body.txt": ("ПИСЬМО\nОт: ООО «Бета»\n\nВ связи с задержкой просим перенести "
                                       "срок поставки по договору № STN-214 на 22.04.2026.\n"),
    "v11_signed_by_parties.txt": "ДОГОВОР ПОСТАВКИ № STN-211\nСтатус: подписан сторонами.\n\n" + _C,
    "v12_lowercase_title.txt": "Договор поставки № STN-212\nСтатус: подписан обеими сторонами.\n\n" + _C,
    "v13_digits_no_spaces.txt": _contract("STN-213", "1.1. Поставщик: ООО «Бета».\n"
                                          "2.1. Общая стоимость поставки составляет 1200000 (один миллион "
                                          "двести тысяч) рублей.\n"
                                          "3.1. Поставщик обязуется поставить товар не позднее 15.04.2026.\n"),
}


def sets() -> dict[str, dict[str, str | bytes]]:
    base = {n: read(n) for n in BASE}
    a1 = "02_amendment_signed.txt"
    c102 = "03_contract.txt"
    stn101 = {"01_contract.txt": base["01_contract.txt"], a1: base[a1]}

    s: dict[str, dict[str, str | bytes]] = {}
    s["a3_later_wins"] = {**stn101, "00_amendment_3.txt":
                          AMENDMENT_TPL.format(no=3, date="05.03.2026", term="20.05.2026")}
    s["a3_conflict"] = {**stn101, "07_amendment_4.txt":
                        AMENDMENT_TPL.format(no=4, date="01.03.2026", term="25.04.2026")}
    s["a3_same_value"] = {**stn101, "08_amendment_5.txt":
                          AMENDMENT_TPL.format(no=5, date="01.03.2026", term="30.04.2026")}
    s["tc10_not_effective"] = {**base, a1: sub(base[a1], "вступает в силу 01.03.2026", "вступает в силу 15.03.2026")}
    s["tc11_draft_status"] = {**base, a1: sub(base[a1], "Статус: подписано обеими сторонами 01.03.2026.",
                                              "Статус: проект, не подписано.")}
    s["tc12_ambiguous_status"] = {**base, a1: sub(base[a1], "Статус: подписано обеими сторонами 01.03.2026.",
                                                  "Статус: подписано Поставщиком 01.03.2026.")}
    s["tc13_contract_only"] = {"01_contract.txt": base["01_contract.txt"]}
    s["tc14_changed_amount"] = {**base, c102: sub(base[c102], "840 000 (восемьсот сорок тысяч)",
                                                  "910 000 (девятьсот десять тысяч)")}
    s["tc15_new_contract"] = {**base, "07_contract.txt": NEW_CONTRACT}
    renamed = {("99_x.txt" if n == a1 else n): t for n, t in base.items()}
    s["tc16_renamed"] = dict(reversed(list(renamed.items())))
    no_term = drop_line(base["01_contract.txt"], "3.1.")
    s["tc17a_missing_term_contract"] = {**base, "01_contract.txt": no_term}
    s["tc17b_missing_term_everywhere"] = {**base, "01_contract.txt": no_term,
                                          a1: drop_line(base[a1], "2. Пункт 3.1")}
    s["tc18_orphan_amendment"] = {a1: base[a1]}
    s["tc19a_digits_vs_words"] = {**base, c102: sub(base[c102], "840 000 (восемьсот", "910 000 (восемьсот")}
    s["tc20_encoding_empty"] = {**base, c102: base[c102].encode("cp1251"), "empty.txt": b""}
    s["robust_wording"] = dict(ROBUST)
    return s


def main() -> None:
    if OUT.exists():
        for d in OUT.iterdir():
            if d.is_dir():
                shutil.rmtree(d)
    for name, files in sets().items():
        folder = OUT / name
        folder.mkdir(parents=True)
        for fname, content in files.items():
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            (folder / fname).write_bytes(data)
    print(f"создано наборов: {len(sets())} в {OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
