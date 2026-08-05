"""Whitelist systemctl helpers for tech-chat remote ops."""

from __future__ import annotations

import json
import subprocess
from typing import Iterable

# Short alias → systemd unit
UNITS: dict[str, str] = {
    "bot": "standup-bot.service",
    "vk": "standup-vk-bot.service",
    "admin": "standup-admin.service",
}

UNIT_TO_ALIAS = {unit: alias for alias, unit in UNITS.items()}


def resolve_unit(alias_or_unit: str) -> str | None:
    key = (alias_or_unit or "").strip().lower()
    if not key:
        return None
    if key in UNITS:
        return UNITS[key]
    if not key.endswith(".service"):
        key = f"{key}.service"
    if key in UNIT_TO_ALIAS:
        return key
    return None


def alias_for_unit(unit: str) -> str | None:
    return UNIT_TO_ALIAS.get(unit)


def _run(cmd: list[str], *, timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out


def unit_active(unit: str) -> str:
    code, out = _run(["systemctl", "is-active", unit], timeout=15)
    if out:
        return out.splitlines()[0].strip()
    return "unknown" if code != 0 else "unknown"


def status_report(aliases: Iterable[str] | None = None) -> str:
    keys = list(aliases) if aliases is not None else list(UNITS.keys())
    lines: list[str] = []
    for alias in keys:
        unit = UNITS.get(alias) or resolve_unit(alias)
        if not unit:
            lines.append(f"{alias}: unknown alias")
            continue
        active = unit_active(unit)
        mark = "✅" if active == "active" else "❌"
        lines.append(f"{mark} {alias} ({unit}): {active}")
    return "\n".join(lines)


def restart_unit(alias_or_unit: str) -> tuple[bool, str]:
    unit = resolve_unit(alias_or_unit)
    if not unit:
        return False, f"Неизвестный сервис: {alias_or_unit}. Можно: bot, vk, admin"
    # -n = non-interactive; needs NOPASSWD sudoers on the VPS.
    code, out = _run(["sudo", "-n", "systemctl", "restart", unit], timeout=60)
    if code == 0:
        active = unit_active(unit)
        return True, f"Перезапуск {unit} выполнен. Сейчас: {active}"
    hint = (
        "Нет права на sudo без пароля. На сервере нужен файл "
        "/etc/sudoers.d/standup-tech (см. deploy/sudoers-standup-tech)."
    )
    detail = out or f"exit={code}"
    if "password" in detail.lower() or "a password is required" in detail.lower():
        return False, f"{hint}\n\n{detail}"
    return False, f"Не удалось перезапустить {unit}:\n{detail}"


def aliases_from_problem_lines(problems: Iterable[str]) -> list[str]:
    found: list[str] = []
    for line in problems:
        unit = (line or "").split(":", 1)[0].strip()
        alias = alias_for_unit(unit)
        if alias and alias not in found:
            found.append(alias)
    return found


def restart_keyboard_json(aliases: Iterable[str]) -> str | None:
    rows = []
    for alias in aliases:
        if alias not in UNITS:
            continue
        rows.append(
            [
                {
                    "text": f"Restart {alias}",
                    "callback_data": f"tech:rst:{alias}",
                }
            ]
        )
    if not rows:
        return None
    rows.append([{"text": "Статус", "callback_data": "tech:status"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)
