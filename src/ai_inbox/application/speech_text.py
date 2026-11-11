import re
from datetime import date
from typing import Any

from num2words import num2words  # type: ignore[import-untyped]

MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def number(value: int, **kwargs: Any) -> str:
    return str(num2words(value, lang="ru", **kwargs))


def plural(value: int, forms: tuple[str, str, str]) -> str:
    if 11 <= value % 100 <= 14:
        return forms[2]
    return forms[0] if value % 10 == 1 else forms[1] if value % 10 in (2, 3, 4) else forms[2]


def prepare_speech(text: str) -> str:
    def money(match: re.Match[str]) -> str:
        whole, decimal = match[1].replace(" ", "").replace("\u00a0", ""), match[2]
        rub, kop = int(whole), int((decimal or "00").ljust(2, "0"))
        return f"{number(rub)} {plural(rub, ('рубль', 'рубля', 'рублей'))} " + (
            f"{number(kop, gender='feminine')} {plural(kop, ('копейка', 'копейки', 'копеек'))}"
            if kop
            else ""
        )

    text = re.sub(r"(?<!\w)(\d[\d \u00a0]*)(?:[,.](\d{1,2}))?\s*(?:руб\.?|₽)(?!\w)", money, text)

    def spoken_date(year: str, month: str, day: str, original: str) -> str:
        try:
            date(int(year), int(month), int(day))
        except ValueError:
            return original
        return f"{number(int(day), to='ordinal', case='genitive')} {MONTHS[int(month)]} " + (
            f"{number(int(year), to='ordinal', case='genitive')} года"
        )

    text = re.sub(
        r"\b(\d{4})-(\d{2})-(\d{2})\b", lambda m: spoken_date(m[1], m[2], m[3], m[0]), text
    )
    text = re.sub(
        r"\b(\d{2})\.(\d{2})\.(\d{4})\b", lambda m: spoken_date(m[3], m[2], m[1], m[0]), text
    )

    def time(match: re.Match[str]) -> str:
        hour, minute = int(match[1]), int(match[2])
        if hour > 23 or minute > 59:
            return match[0]
        return f"{number(hour)} {plural(hour, ('час', 'часа', 'часов'))} " + (
            f"{number(minute, gender='feminine')} {plural(minute, ('минута', 'минуты', 'минут'))}"
            if minute
            else "ровно"
        )

    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", time, text)
    for abbreviation, expanded in {
        "т. д.": "так далее",
        "т. п.": "тому подобное",
        "ООО": "общество с ограниченной ответственностью",
        "ASR": "распознавание речи",
    }.items():
        text = text.replace(abbreviation, expanded)
    text = re.sub(r"https?://\S+", "ссылка в приложении", text)
    text = re.sub(r"\b[a-f0-9]{8}-[a-f0-9-]{27}\b", "идентификатор в приложении", text)
    return re.sub(r"[\*_#`]", "", text).strip()
