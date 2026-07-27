"""Heuristics for free-text messages outside booking flows."""

from __future__ import annotations

from collections import Counter


def is_meaningful_free_text(text: str | None) -> bool:
    """Осмысленный текст от 10 символов; короткий спам и абракадабру отсекаем."""
    text = (text or "").strip()
    if len(text) < 10:
        return False
    if text.startswith("/"):
        return False

    letters = [c for c in text.lower() if c.isalpha()]
    if len(letters) < 6:
        return False

    unique_ratio = len(set(letters)) / len(letters)
    if unique_ratio < 0.25:
        return False

    most_common = Counter(letters).most_common(1)[0][1]
    if most_common / len(letters) > 0.6:
        return False

    vowels = set("аеёиоуыэюяaeiouy")
    if sum(1 for c in letters if c in vowels) == 0:
        return False

    return True
