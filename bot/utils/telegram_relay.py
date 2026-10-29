"""Scoped Telegram Bot API relay transport.

The relay is a TCP tunnel, not an HTTP proxy. Keep Telegram URLs unchanged so
TLS SNI and certificate validation still use api.telegram.org.

Modes (TELEGRAM_RELAY_MODE):
- off: always direct, even if TELEGRAM_RELAY_HOST is set
- always: every api.telegram.org:443 request goes through the tunnel
- fallback: try direct first; on network errors retry through the tunnel
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver

TELEGRAM_API_HOST = "api.telegram.org"
TELEGRAM_API_PORT = 443
DEFAULT_RELAY_LIMIT = 4
DEFAULT_DIRECT_LIMIT = 100
DEFAULT_DIRECT_CONNECT_TIMEOUT = 5.0
# aiogram default. Must stay set: ClientTimeout(total=None) waits forever
# after TCP connect, so fallback never switches to the tunnel.
DEFAULT_REQUEST_TOTAL_TIMEOUT = 60.0
DEFAULT_FAILS_BEFORE_SKIP = 3
DEFAULT_DIRECT_COOLDOWN_SEC = 60.0
_QUIET_OK_METHODS = {"getUpdates"}
_TOKEN_IN_TEXT = re.compile(r"bot\d+:[A-Za-z0-9_-]+", re.IGNORECASE)

logger = logging.getLogger(__name__)
_journal_lock = threading.Lock()
_circuit_lock = threading.Lock()


@dataclass(frozen=True)
class TelegramRelaySettings:
    host: str
    port: int

    @property
    def enabled(self) -> bool:
        return bool(self.host)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


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


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def telegram_relay_settings() -> TelegramRelaySettings:
    host = (os.getenv("TELEGRAM_RELAY_HOST") or "").strip()
    if not host:
        return TelegramRelaySettings(host="", port=0)
    port = _int_env("TELEGRAM_RELAY_PORT", 18443)
    return TelegramRelaySettings(host=host, port=port)


def telegram_relay_mode() -> str:
    """off | always | fallback. Empty TELEGRAM_RELAY_HOST forces off."""
    if not telegram_relay_settings().enabled:
        return "off"
    raw = (os.getenv("TELEGRAM_RELAY_MODE") or "fallback").strip().lower()
    if raw in {"off", "always", "fallback"}:
        return raw
    raise RuntimeError("TELEGRAM_RELAY_MODE must be off, always, or fallback")


def telegram_api_connection_limit(*, relay: bool) -> int:
    if relay:
        return _int_env("TELEGRAM_RELAY_LIMIT", DEFAULT_RELAY_LIMIT)
    raw = (os.getenv("TELEGRAM_DIRECT_LIMIT") or "").strip()
    if raw:
        return _int_env("TELEGRAM_DIRECT_LIMIT", DEFAULT_DIRECT_LIMIT)
    return DEFAULT_DIRECT_LIMIT


def telegram_direct_connect_timeout() -> float:
    return _float_env("TELEGRAM_DIRECT_CONNECT_TIMEOUT", DEFAULT_DIRECT_CONNECT_TIMEOUT)


def telegram_transport_journal_path() -> Path:
    raw = (os.getenv("TELEGRAM_TRANSPORT_JOURNAL") or "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else _project_root() / path
    return _project_root() / "data" / "telegram_transport.jsonl"


def telegram_transport_summary_path() -> Path:
    raw = (os.getenv("TELEGRAM_TRANSPORT_SUMMARY") or "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else _project_root() / path
    journal = telegram_transport_journal_path()
    return journal.with_name("telegram_transport_summary.txt")


_EVENT_RU = {
    "direct_ok": "напрямую ок",
    "direct_fail": "напрямую не вышло",
    "direct_recovered": "снова напрямую",
    "relay_ok": "ушло через мост",
    "relay_fail": "мост тоже не вышел",
    "circuit_open": "прямые попытки на паузе, пока только мост",
}


class _TransportStats:
    def __init__(self) -> None:
        self.direct_ok = 0
        self.direct_fail = 0
        self.relay_ok = 0
        self.relay_fail = 0
        self.last_event = ""
        self.last_method = ""
        self.last_error = ""
        self.last_ts = ""
        self.started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


_stats = _TransportStats()
_summary_lock = threading.Lock()
_last_summary_write = 0.0


def _current_path_label() -> str:
    if not _circuit.should_try_direct():
        return "мост (прямые попытки на паузе ~1 мин)"
    if _circuit.using_relay:
        return "мост (после сбоя, следующий запрос снова пробует напрямую)"
    return "напрямую"


def format_transport_summary(*, last_events: list[dict[str, Any]] | None = None) -> str:
    total = _stats.direct_ok + _stats.relay_ok
    if total:
        bridge_pct = f"{100.0 * _stats.relay_ok / total:.1f}%"
    else:
        bridge_pct = "пока нет запросов"
    lines = [
        "Telegram транспорт",
        f"Обновлено: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}",
        f"Режим: {telegram_relay_mode()}",
        f"Сейчас: {_current_path_label()}",
        f"С запуска процесса: напрямую {_stats.direct_ok}, через мост {_stats.relay_ok}",
        f"Сбои напрямую: {_stats.direct_fail}; сбои моста: {_stats.relay_fail}",
        f"Доля через мост: {bridge_pct}",
        "",
        "Как часто в мост: не по расписанию.",
        "Каждый запрос сначала идёт напрямую (до 5 сек на соединение).",
        "Если Beget отвечает — мост не используется.",
        "Если 3 раза подряд напрямую не вышло — около минуты только мост,",
        "потом снова проба напрямую.",
    ]
    if _stats.last_event:
        label = _EVENT_RU.get(_stats.last_event, _stats.last_event)
        last = f"Последнее: {label}"
        if _stats.last_method:
            last += f" ({_stats.last_method})"
        if _stats.last_ts:
            last += f" в {_stats.last_ts}"
        lines.append("")
        lines.append(last)
        if _stats.last_error:
            lines.append(f"Ошибка: {_stats.last_error}")
    events = last_events if last_events is not None else _read_last_journal_events(8)
    if events:
        lines.append("")
        lines.append("Последние события:")
        for row in events:
            label = _EVENT_RU.get(str(row.get("event") or ""), str(row.get("event") or ""))
            ts = str(row.get("ts") or "")
            method = str(row.get("method") or "")
            piece = f"- {ts} {label}"
            if method:
                piece += f" · {method}"
            err = str(row.get("error") or "")
            if err:
                piece += f" · {err}"
            lines.append(piece)
    return "\n".join(lines) + "\n"


def _read_last_journal_events(limit: int) -> list[dict[str, Any]]:
    path = telegram_transport_journal_path()
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in raw[-max(limit, 1) :]:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _write_transport_summary(*, force: bool = False) -> None:
    global _last_summary_write
    now = time.monotonic()
    with _summary_lock:
        if not force and now - _last_summary_write < 10:
            return
        _last_summary_write = now
        text = format_transport_summary()
        path = telegram_transport_summary_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            logger.warning("telegram transport summary write failed")


def _note_stats(event: str, *, method: str = "", error: str = "") -> None:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    if event in {"direct_ok", "direct_recovered"}:
        _stats.direct_ok += 1
    elif event == "direct_fail":
        _stats.direct_fail += 1
    elif event == "relay_ok":
        _stats.relay_ok += 1
    elif event == "relay_fail":
        _stats.relay_fail += 1
    _stats.last_event = event
    _stats.last_method = method
    _stats.last_error = _sanitize_error(error) if error else ""
    _stats.last_ts = ts


def _sanitize_error(exc: BaseException | str) -> str:
    text = str(exc)
    text = _TOKEN_IN_TEXT.sub("bot<redacted>", text)
    return text[:300]


def log_transport_event(
    event: str,
    *,
    method: str = "",
    source: str = "",
    error: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "event": event,
        "method": method,
        "source": source,
        "mode": telegram_relay_mode(),
    }
    if error:
        payload["error"] = _sanitize_error(error)
    if extra:
        payload.update(extra)
    line = json.dumps(payload, ensure_ascii=False)
    path = telegram_transport_journal_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _journal_lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        logger.warning("telegram transport journal write failed")
    log_fn = logger.warning if event.endswith("_fail") or event in {"circuit_open"} else logger.info
    log_fn("tg transport %s method=%s source=%s", event, method or "-", source or "-")
    _note_stats(event, method=method, error=error if isinstance(error, str) else str(error or ""))
    _write_transport_summary(force=event.endswith("_fail") or event in {"circuit_open", "direct_recovered", "relay_ok"})


class _DirectCircuit:
    def __init__(self) -> None:
        self.fails = 0
        self.skip_until = 0.0
        self.using_relay = False
        self.ok_count = 0

    def should_try_direct(self) -> bool:
        with _circuit_lock:
            return time.monotonic() >= self.skip_until

    def on_ok(self, method: str, source: str) -> None:
        with _circuit_lock:
            recovered = self.using_relay or self.fails > 0
            self.fails = 0
            self.skip_until = 0.0
            self.using_relay = False
            self.ok_count += 1
            ok_count = self.ok_count
        if recovered:
            log_transport_event(
                "direct_recovered",
                method=method,
                source=source,
                extra={"ok_count": ok_count},
            )
        elif method not in _QUIET_OK_METHODS and (ok_count <= 5 or ok_count % 100 == 0):
            log_transport_event(
                "direct_ok",
                method=method,
                source=source,
                extra={"ok_count": ok_count},
            )
        else:
            _stats.direct_ok += 1
            _write_transport_summary()

    def on_fail(self, method: str, source: str, exc: BaseException) -> None:
        threshold = _int_env("TELEGRAM_DIRECT_FAILS_BEFORE_SKIP", DEFAULT_FAILS_BEFORE_SKIP)
        cooldown = _float_env("TELEGRAM_DIRECT_COOLDOWN_SEC", DEFAULT_DIRECT_COOLDOWN_SEC)
        opened = False
        with _circuit_lock:
            self.fails += 1
            self.using_relay = True
            if self.fails >= threshold:
                self.skip_until = time.monotonic() + cooldown
                self.fails = 0
                opened = True
        log_transport_event(
            "direct_fail",
            method=method,
            source=source,
            error=exc,
        )
        if opened:
            log_transport_event(
                "circuit_open",
                method=method,
                source=source,
                extra={"cooldown_sec": cooldown},
            )


_circuit = _DirectCircuit()


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


def _make_aiogram_session(*, relay: bool):
    from aiogram.client.session.aiohttp import AiohttpSession

    session = AiohttpSession(limit=telegram_api_connection_limit(relay=relay))
    if relay:
        settings = telegram_relay_settings()
        if settings.enabled:
            session._connector_init["resolver"] = TelegramRelayResolver(settings)
    return session


class TelegramRelayAiohttpSession:
    """Factory wrapper for aiogram's AiohttpSession with a scoped resolver."""

    @staticmethod
    def create():
        mode = telegram_relay_mode()
        if mode == "always":
            return _make_aiogram_session(relay=True)
        if mode == "fallback":
            return _new_fallback_session()
        return _make_aiogram_session(relay=False)


def _new_fallback_session():
    """Try direct Telegram API, then the local SSH tunnel."""
    from aiogram.client.session.base import BaseSession
    from aiogram.exceptions import TelegramNetworkError

    class TelegramFallbackSession(BaseSession):
        def __init__(self) -> None:
            super().__init__()
            self._direct = _make_aiogram_session(relay=False)
            self._relay = _make_aiogram_session(relay=True)

        async def make_request(self, bot, method, timeout: int | None = None):
            api_method = getattr(method, "__api_method__", type(method).__name__)
            source = "aiogram"
            if _circuit.should_try_direct():
                try:
                    result = await self._direct.make_request(
                        bot, method, timeout=_direct_timeout(timeout)
                    )
                    _circuit.on_ok(api_method, source)
                    return result
                except TelegramNetworkError as exc:
                    _circuit.on_fail(api_method, source, exc)
            try:
                result = await self._relay.make_request(bot, method, timeout=timeout)
            except Exception as exc:
                log_transport_event("relay_fail", method=api_method, source=source, error=exc)
                raise
            if api_method not in _QUIET_OK_METHODS:
                log_transport_event(
                    "relay_ok", method=api_method, source=source, extra={"via": "fallback"}
                )
            else:
                _note_stats("relay_ok", method=api_method)
                _write_transport_summary()
            return result

        async def stream_content(
            self,
            url: str,
            headers: dict[str, Any] | None = None,
            timeout: int = 30,
            chunk_size: int = 65536,
            raise_for_status: bool = True,
        ):
            source = "aiogram"
            method = "download_file"
            if _circuit.should_try_direct():
                try:
                    async for chunk in self._direct.stream_content(
                        url,
                        headers=headers,
                        timeout=timeout,
                        chunk_size=chunk_size,
                        raise_for_status=raise_for_status,
                    ):
                        yield chunk
                    _circuit.on_ok(method, source)
                    return
                except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                    _circuit.on_fail(method, source, exc)
            try:
                async for chunk in self._relay.stream_content(
                    url,
                    headers=headers,
                    timeout=timeout,
                    chunk_size=chunk_size,
                    raise_for_status=raise_for_status,
                ):
                    yield chunk
            except Exception as exc:
                log_transport_event("relay_fail", method=method, source=source, error=exc)
                raise
            log_transport_event("relay_ok", method=method, source=source, extra={"via": "fallback"})

        async def close(self) -> None:
            await self._direct.close()
            await self._relay.close()

    return TelegramFallbackSession()


def _direct_timeout(timeout: int | float | None):
    """Connect fast, but never wait forever for a response body.

    Passing ClientTimeout(total=None) into aiogram replaces its 60s default
    and can hang sendMessage after a half-open TCP connect.
    """
    from aiohttp import ClientTimeout

    connect = telegram_direct_connect_timeout()
    if timeout is None:
        total = DEFAULT_REQUEST_TOTAL_TIMEOUT
    else:
        try:
            total = float(timeout)
        except (TypeError, ValueError):
            total = DEFAULT_REQUEST_TOTAL_TIMEOUT
        if total <= 0:
            total = DEFAULT_REQUEST_TOTAL_TIMEOUT
    connect = min(connect, total)
    return ClientTimeout(
        total=total,
        connect=connect,
        sock_connect=connect,
        sock_read=total,
    )


def create_telegram_bot(token: str, **kwargs):
    """Create an aiogram Bot honoring TELEGRAM_RELAY_MODE."""
    from aiogram import Bot

    if "session" not in kwargs:
        kwargs["session"] = TelegramRelayAiohttpSession.create()
    return Bot(token=token, **kwargs)


def telegram_aiohttp_connector(*, relay: bool) -> aiohttp.TCPConnector | None:
    if not relay:
        return None
    settings = telegram_relay_settings()
    if not settings.enabled:
        return None
    return aiohttp.TCPConnector(
        resolver=TelegramRelayResolver(settings),
        limit=telegram_api_connection_limit(relay=True),
        ttl_dns_cache=3600,
    )


def create_telegram_aiohttp_session(**kwargs: Any) -> aiohttp.ClientSession:
    """Create aiohttp session for Telegram Bot API. Used by always/off modes."""
    mode = telegram_relay_mode()
    if "connector" not in kwargs:
        connector = telegram_aiohttp_connector(relay=mode == "always")
        if connector is not None:
            kwargs["connector"] = connector
    return aiohttp.ClientSession(**kwargs)


async def telegram_aiohttp_post_json(
    url: str,
    *,
    json_body: dict[str, Any],
    timeout: aiohttp.ClientTimeout | None = None,
    source: str = "aiohttp",
) -> dict[str, Any]:
    """POST JSON to Telegram API with optional direct→relay fallback."""
    timeout = timeout or aiohttp.ClientTimeout(total=15, connect=telegram_direct_connect_timeout())
    method = url.rsplit("/", 1)[-1] if url else "post"
    method = method.split("?", 1)[0]
    if method.startswith("bot"):
        method = "sendMessage"
    mode = telegram_relay_mode()

    async def _once(*, relay: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        connector = telegram_aiohttp_connector(relay=relay)
        if connector is not None:
            kwargs["connector"] = connector
        async with aiohttp.ClientSession(**kwargs) as session:
            async with session.post(url, json=json_body, timeout=timeout) as resp:
                return await resp.json(content_type=None)

    if mode != "fallback":
        return await _once(relay=mode == "always")

    if _circuit.should_try_direct():
        try:
            data = await _once(relay=False)
            _circuit.on_ok(method, source)
            return data
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            _circuit.on_fail(method, source, exc)
    try:
        data = await _once(relay=True)
    except Exception as exc:
        log_transport_event("relay_fail", method=method, source=source, error=exc)
        raise
    log_transport_event("relay_ok", method=method, source=source, extra={"via": "fallback"})
    return data


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


def _https_post_form(
    token: str,
    method: str,
    fields: dict[str, str],
    *,
    timeout: float,
    relay: TelegramRelaySettings | None,
) -> tuple[int, bytes]:
    context = ssl.create_default_context()
    if relay is not None:
        conn: http.client.HTTPSConnection = _TelegramRelayHTTPSConnection(
            TELEGRAM_API_HOST,
            relay=relay,
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


def telegram_api_post_form(
    token: str,
    method: str,
    fields: dict[str, str],
    *,
    timeout: float = 15,
) -> tuple[int, bytes]:
    """POST form data to Telegram Bot API, honoring TELEGRAM_RELAY_MODE."""
    settings = telegram_relay_settings()
    mode = telegram_relay_mode()
    source = "sync"
    if mode == "always" and settings.enabled:
        return _https_post_form(token, method, fields, timeout=timeout, relay=settings)
    if mode != "fallback" or not settings.enabled:
        return _https_post_form(token, method, fields, timeout=timeout, relay=None)

    if _circuit.should_try_direct():
        try:
            result = _https_post_form(token, method, fields, timeout=timeout, relay=None)
            _circuit.on_ok(method, source)
            return result
        except (TimeoutError, OSError) as exc:
            _circuit.on_fail(method, source, exc)
    try:
        result = _https_post_form(token, method, fields, timeout=timeout, relay=settings)
    except Exception as exc:
        log_transport_event("relay_fail", method=method, source=source, error=exc)
        raise
    log_transport_event("relay_ok", method=method, source=source, extra={"via": "fallback"})
    return result
