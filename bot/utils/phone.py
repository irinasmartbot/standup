"""Проверка и нормализация телефона при ручном вводе."""

from __future__ import annotations

import re

# Разрешены цифры, пробелы, скобки, дефисы, точки и один ведущий +
_PHONE_PATTERN = re.compile(r"^\+?[\d\s\-().]+$")

PHONE_INVALID_TEXT = (
    "Нужен корректный номер телефона с кодом страны — без букв и лишних символов.\n"
    "Пример: <b>+79001234567</b>\n\n"
    "Отправьте контакт кнопкой или введите номер ещё раз 👇"
)


def normalize_phone(phone: str | None) -> str | None:
    """Возвращает нормализованный номер или None, если формат некорректный."""
    raw = (phone or "").strip()
    if not raw or not _PHONE_PATTERN.fullmatch(raw):
        return None

    digits = "".join(ch for ch in raw if ch.isdigit())
    # E.164: код страны + номер, обычно 10–15 цифр
    if not (10 <= len(digits) <= 15):
        return None

    # Отсекаем явный мусор вроде 7777777777
    if len(set(digits)) < 3:
        return None

    if raw.startswith("+"):
        return "+" + digits
    return digits


def is_valid_phone(phone: str | None) -> bool:
    return normalize_phone(phone) is not None
