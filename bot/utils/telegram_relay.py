"""Scoped Telegram Bot API relay transport.

The relay is a TCP tunnel, not an HTTP proxy. Keep Telegram URLs unchanged so
TLS SNI and certificate validation still use api.telegram.org.
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import urllib.parse
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver

TELEGRAM_API_HOST = "api.telegram.org"
TELEGRAM_API_PORT = 443
DEFAULT_RELAY_LIMIT = 4
DEFAULT_DIRECT_LIMIT = 100


@dataclass(frozen=True)
class TelegramRelaySettings:
    host: str
    port: int

    @property
    def enabled(self) -> bool:
        return bool(self.host)


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def telegram_relay_settings() -> TelegramRelaySettings:
    host = (os.getenv("TELEGRAM_RELAY_HOST") or "").strip()
    if not host:
        return TelegramRelaySettings(host="", port=0)
    port = _int_env("TELEGRAM_RELAY_PORT", 18443)
    return TelegramRelaySettings(host=host, port=port)


def telegram_api_connection_limit() -> int:
    default = DEFAULT_RELAY_LIMIT if telegram_relay_settings().enabled else DEFAULT_DIRECT_LIMIT
    return _int_env("TELEGRAM_RELAY_LIMIT", default)


class TelegramRelayResolver(AbstractResolver):
    """Resolve only api.telegram.org:443 to the local tunnel endpoint."""

    def __init__(self, settings: TelegramRelaySettings | None = None) -> None:
        self._settings = settings or telegram_relay_settings()
        self._default: AbstractResolver | None = None

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        if (
            self._settings.enabled
            and host.lower() == TELEGRAM_API_HOST
            and int(port) == TELEGRAM_API_PORT
        ):
            return [
                {
                    "hostname": host,
                    "host": self._settings.host,
                    "port": self._settings.port,
                    "family": socket.AF_INET,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            ]
        if self._default is None:
            self._default = aiohttp.DefaultResolver()
        return await self._default.resolve(host, port, family)

    async def close(self) -> None:
        if self._default is not None:
            await self._default.close()


class TelegramRelayAiohttpSession:
    """Factory wrapper for aiogram's AiohttpSession with a scoped resolver."""

    @staticmethod
    def create():
        from aiogram.client.session.aiohttp import AiohttpSession

        session = AiohttpSession(limit=telegram_api_connection_limit())
        settings = telegram_relay_settings()
        if settings.enabled:
            session._connector_init["resolver"] = TelegramRelayResolver(settings)
        return session


def create_telegram_bot(token: str, **kwargs):
    """Create an aiogram Bot that uses the relay when TELEGRAM_RELAY_HOST is set."""
    from aiogram import Bot

    settings = telegram_relay_settings()
    if settings.enabled and "session" not in kwargs:
        kwargs["session"] = TelegramRelayAiohttpSession.create()
    return Bot(token=token, **kwargs)


def telegram_aiohttp_connector() -> aiohttp.TCPConnector | None:
    settings = telegram_relay_settings()
    if not settings.enabled:
        return None
    return aiohttp.TCPConnector(
        resolver=TelegramRelayResolver(settings),
        limit=telegram_api_connection_limit(),
        ttl_dns_cache=3600,
    )


def create_telegram_aiohttp_session(**kwargs: Any) -> aiohttp.ClientSession:
    """Create aiohttp session for direct Telegram Bot API calls."""
    if "connector" not in kwargs:
        connector = telegram_aiohttp_connector()
        if connector is not None:
            kwargs["connector"] = connector
    return aiohttp.ClientSession(**kwargs)


class _TelegramRelayHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        relay: TelegramRelaySettings,
        timeout: float | None,
        context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(host, port=TELEGRAM_API_PORT, timeout=timeout, context=context)
        self._relay = relay

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._relay.host, self._relay.port),
            self.timeout,
            self.source_address,
        )
        context = self._context or ssl.create_default_context()
        self.sock = context.wrap_socket(self.sock, server_hostname=self.host)


def telegram_api_post_form(
    token: str,
    method: str,
    fields: dict[str, str],
    *,
    timeout: float = 15,
) -> tuple[int, bytes]:
    """POST form data to Telegram Bot API, optionally via the relay."""
    settings = telegram_relay_settings()
    context = ssl.create_default_context()
    if settings.enabled:
        conn: http.client.HTTPSConnection = _TelegramRelayHTTPSConnection(
            TELEGRAM_API_HOST,
            relay=settings,
            timeout=timeout,
            context=context,
        )
    else:
        conn = http.client.HTTPSConnection(
            TELEGRAM_API_HOST,
            TELEGRAM_API_PORT,
            timeout=timeout,
            context=context,
        )
    try:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        conn.request(
            "POST",
            f"/bot{token}/{method}",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()
