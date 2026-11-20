"""HTTP-клиент реестра сертификатов на сайте.

Успех: {"ok": true, "data": {...}}.
Ошибка: {"ok": false, "error": "...", "message": "..."}.
Сетевой сбой и 5xx не считаются отказом сертификата.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from bot.config import CERT_API_BASE, CERT_BOT_TOKEN, CERT_STAFF_TOKEN

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20)


class RegistryNotConfigured(Exception):
    """Ключ бота или сотрудника не задан."""


class RegistryTemporary(Exception):
    """Таймаут или 5xx: исход неизвестен, тот же идентификатор можно повторить."""


class RegistryRefusal(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message or code)
        self.code = code or "refused"
        self.message = message or ""
        self.http_status = http_status


def failure_kind(http_status: int) -> str:
    if http_status in {408, 429} or http_status >= 500 or http_status <= 0:
        return "temporary"
    if 400 <= http_status < 500:
        return "definite"
    return "temporary"


def _auth_error(status: int) -> bool:
    return status in {401, 403}


class CertificateRegistry:
    def __init__(self, base: str | None = None, bot_token: str | None = None, staff_token: str | None = None):
        self.base = (base if base is not None else CERT_API_BASE).rstrip("/")
        self.bot_token = CERT_BOT_TOKEN if bot_token is None else bot_token
        self.staff_token = CERT_STAFF_TOKEN if staff_token is None else staff_token

    def configured(self, *, staff: bool = False) -> bool:
        return bool(self.staff_token if staff else self.bot_token)

    async def resolve(self, *, handoff_token: str = "", code: str = "") -> dict[str, Any]:
        payload: dict[str, str] = {}
        if handoff_token:
            payload["handoff_token"] = handoff_token
        elif code:
            payload["code"] = code
        else:
            raise RegistryRefusal("missing_code", "Нужен номер или ссылка")
        return await self._post("bot/resolve", payload, staff=False)

    async def claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("bot/claim", payload, staff=False)

    async def review(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("bot/review", payload, staff=True)

    async def reserve(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("bot/reserve", payload, staff=False)

    async def booking_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("bot/booking-status", payload, staff=False)

    async def confirm(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("bot/confirm", payload, staff=False)

    async def cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("bot/cancel", payload, staff=False)

    async def expire(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("bot/expire", payload, staff=False)

    async def _post(self, path: str, payload: dict[str, Any], *, staff: bool) -> dict[str, Any]:
        token = self.staff_token if staff else self.bot_token
        if not token:
            raise RegistryNotConfigured()
        url = f"{self.base}/{path.lstrip('/')}"
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                ) as resp:
                    status = resp.status
                    raw = await resp.text()
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            logger.warning("certificate registry temporary %s: %s", path, type(exc).__name__)
            raise RegistryTemporary(str(exc)) from exc

        data = _parse_json(raw)
        if isinstance(data, dict) and data.get("ok") is True:
            inner = data.get("data")
            return inner if isinstance(inner, dict) else {}

        code = ""
        message = ""
        if isinstance(data, dict):
            code = str(data.get("error") or "")
            message = str(data.get("message") or "")
        if failure_kind(status) == "temporary":
            logger.warning("certificate registry temporary %s status=%s code=%s", path, status, code)
            raise RegistryTemporary(message or code or f"http {status}")
        if _auth_error(status):
            logger.error("certificate registry auth failed %s status=%s", path, status)
            raise RegistryNotConfigured()
        logger.info("certificate registry refused %s status=%s code=%s", path, status, code)
        raise RegistryRefusal(code, message, status)


def _parse_json(raw: str) -> Any:
    import json

    try:
        return json.loads(raw or "")
    except (TypeError, ValueError):
        return None


registry = CertificateRegistry()
