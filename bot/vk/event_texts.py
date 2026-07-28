"""Тексты карточек мероприятий для VK (HTML → format_data на отправке)."""

from __future__ import annotations

from typing import Any

from bot.utils.ticket import format_date


def host_lines(host: str) -> list[str]:
    text = (host or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    return [line.strip(" -–—\t") for line in text.split("\n") if line.strip()]


def host_block(host: str, *, title: str = "Кто выступает") -> str:
    lines = host_lines(host)
    if not lines:
        return ""
    body = "\n".join(f"🎤 {line}" for line in lines)
    return f"\n<b>{title}:</b>\n{body}"


def event_card_text(event: dict[str, Any], *, host_title: str = "Кто выступает") -> str:
    location_line = " ".join(
        part for part in [event.get("time") or "", event.get("location") or ""] if part
    ).strip()
    if not location_line:
        location_line = (event.get("time") or "").strip()
    parts = [
        f"<b>{format_date(event.get('date') or '')}</b>",
        event.get("weekday") or "",
        "",
        f"<b>{location_line}</b>" if location_line else "",
        event.get("address") or "",
        event.get("description") or "",
    ]
    block = host_block(event.get("host") or "", title=host_title)
    if block:
        while parts and parts[-1] == "":
            parts.pop()
        parts.append(block)
    return "\n".join(part for part in parts if part is not None).strip()


def best_event_text(event: dict[str, Any]) -> str:
    return event_card_text(event, host_title="Кто выступает")


def hitloto_event_text(event: dict[str, Any]) -> str:
    lines = host_lines(event.get("host") or "")
    title = "Ведущие" if len(lines) > 1 else "Ведущий"
    return event_card_text(event, host_title=title)
