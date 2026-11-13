"""Yandex Metrica offline conversions.

Failures here must never break booking flows.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_COUNTER_ID = "88707048"
DEFAULT_BOOKING_CREATED_TARGET = "booking_created"
DEFAULT_BOOKING_CREATED_TG_TARGET = "booking_created_tg"
DEFAULT_BOOKING_CREATED_VK_TARGET = "booking_created_vk"
DEFAULT_BOT_OPEN_TARGET = "bot_open"
DEFAULT_BOT_OPEN_TG_TARGET = "bot_open_tg"
DEFAULT_BOT_OPEN_VK_TARGET = "bot_open_vk"


def _counter_id() -> str:
    return (
        os.getenv("YANDEX_METRIKA_COUNTER_ID")
        or os.getenv("METRIKA_COUNTER_ID")
        or DEFAULT_COUNTER_ID
    ).strip()


def _oauth_token() -> str:
    return (
        os.getenv("YANDEX_METRIKA_OAUTH_TOKEN")
        or os.getenv("YANDEX_METRIKA_TOKEN")
        or os.getenv("METRIKA_OAUTH_TOKEN")
        or ""
    ).strip()


def _booking_created_target() -> str:
    return (
        os.getenv("YANDEX_METRIKA_BOOKING_CREATED_TARGET")
        or DEFAULT_BOOKING_CREATED_TARGET
    ).strip()


def _booking_created_channel_target(channel: str) -> str:
    value = (channel or "").strip().casefold()
    if value in {"telegram", "tg"}:
        return (
            os.getenv("YANDEX_METRIKA_BOOKING_CREATED_TG_TARGET")
            or DEFAULT_BOOKING_CREATED_TG_TARGET
        ).strip()
    if value in {"vkontakte", "vk", "vkontakte_bot"}:
        return (
            os.getenv("YANDEX_METRIKA_BOOKING_CREATED_VK_TARGET")
            or DEFAULT_BOOKING_CREATED_VK_TARGET
        ).strip()
    return ""


def _bot_open_target() -> str:
    return (
        os.getenv("YANDEX_METRIKA_BOT_OPEN_TARGET")
        or DEFAULT_BOT_OPEN_TARGET
    ).strip()


def _bot_open_channel_target(channel: str) -> str:
    value = (channel or "").strip().casefold()
    if value in {"telegram", "tg"}:
        return (
            os.getenv("YANDEX_METRIKA_BOT_OPEN_TG_TARGET")
            or DEFAULT_BOT_OPEN_TG_TARGET
        ).strip()
    if value in {"vkontakte", "vk", "vkontakte_bot"}:
        return (
            os.getenv("YANDEX_METRIKA_BOT_OPEN_VK_TARGET")
            or DEFAULT_BOT_OPEN_VK_TARGET
        ).strip()
    return ""


def _conversion_timestamp(value: datetime | None = None) -> int:
    ts = int((value or datetime.now()).timestamp())
    # Metrica rejects future timestamps; give clock skew a small buffer.
    return min(ts, int(time.time()) - 1)


def _csv_payload(
    *,
    target: str,
    client_id: str,
    created_at: datetime | None = None,
) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["ClientId", "Target", "DateTime"])
    writer.writeheader()
    writer.writerow(
        {
            "ClientId": client_id,
            "Target": target,
            "DateTime": _conversion_timestamp(created_at),
        }
    )
    return buf.getvalue()


def _multipart_body(csv_text: str) -> tuple[bytes, str]:
    boundary = f"----standup-metrika-{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}\r\n",
        'Content-Disposition: form-data; name="file"; filename="offline-conversions.csv"\r\n',
        "Content-Type: text/csv; charset=utf-8\r\n\r\n",
        csv_text,
        f"\r\n--{boundary}--\r\n",
    ]
    body = "".join(parts).encode("utf-8")
    return body, boundary


def upload_offline_conversion(
    *,
    target: str,
    client_id: str,
    created_at: datetime | None = None,
    comment: str = "",
    context: dict[str, Any] | None = None,
) -> bool:
    """Upload one ClientId-based offline conversion to Yandex Metrica."""
    client_id = (client_id or "").strip()
    if not client_id:
        logger.info("Metrica offline conversion skipped: no ClientId context=%s", context or {})
        return False

    counter_id = _counter_id()
    token = _oauth_token()
    if not counter_id or not token:
        logger.info(
            "Metrica offline conversion skipped: counter/token missing target=%s client_id=%s context=%s",
            target,
            client_id,
            context or {},
        )
        return False

    target = (target or "").strip()
    if not target:
        logger.info("Metrica offline conversion skipped: no target client_id=%s", client_id)
        return False

    csv_text = _csv_payload(target=target, client_id=client_id, created_at=created_at)
    body, boundary = _multipart_body(csv_text)
    query = urllib.parse.urlencode(
        {
            "type": "BASIC",
            "comment": (comment or target)[:255],
        }
    )
    url = (
        f"https://api-metrika.yandex.net/management/v1/counter/"
        f"{counter_id}/offline_conversions/upload?{query}"
    )
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"OAuth {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "standup-bot/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw else {}
        uploading = data.get("uploading") if isinstance(data, dict) else {}
        logger.info(
            "Metrica offline conversion uploaded target=%s client_id=%s upload_id=%s status=%s context=%s",
            target,
            client_id,
            (uploading or {}).get("id"),
            (uploading or {}).get("status"),
            context or {},
        )
        return True
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:500]
        logger.warning(
            "Metrica offline conversion HTTP error status=%s target=%s client_id=%s body=%s context=%s",
            exc.code,
            target,
            client_id,
            body_text,
            context or {},
        )
    except Exception:
        logger.exception(
            "Metrica offline conversion failed target=%s client_id=%s context=%s",
            target,
            client_id,
            context or {},
        )
    return False


def _queue_goal(
    *,
    target: str,
    client_id: str,
    created_at: datetime | None = None,
    comment: str = "",
    context: dict[str, Any] | None = None,
    thread_name: str = "metrika-goal",
) -> None:
    thread = threading.Thread(
        target=upload_offline_conversion,
        kwargs={
            "target": target,
            "client_id": client_id,
            "created_at": created_at,
            "comment": comment or target,
            "context": context or {},
        },
        name=thread_name,
        daemon=True,
    )
    thread.start()


def queue_bot_open_goal(
    *,
    client_id: str,
    channel: str = "",
    created_at: datetime | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Send shared bot_open plus channel goal bot_open_tg / bot_open_vk."""
    payload = dict(context or {})
    if channel:
        payload.setdefault("channel", channel)
    _queue_goal(
        target=_bot_open_target(),
        client_id=client_id,
        created_at=created_at,
        comment="bot_open",
        context=payload,
        thread_name="metrika-bot-open",
    )
    channel_target = _bot_open_channel_target(channel)
    if channel_target:
        _queue_goal(
            target=channel_target,
            client_id=client_id,
            created_at=created_at,
            comment=channel_target,
            context=payload,
            thread_name=f"metrika-{channel_target}",
        )


def queue_booking_created_goal(
    *,
    client_id: str,
    channel: str = "",
    created_at: datetime | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Send shared booking_created plus channel goal booking_created_tg / booking_created_vk."""
    payload = dict(context or {})
    if channel:
        payload.setdefault("channel", channel)
    _queue_goal(
        target=_booking_created_target(),
        client_id=client_id,
        created_at=created_at,
        comment="booking_created",
        context=payload,
        thread_name="metrika-booking-created",
    )
    channel_target = _booking_created_channel_target(channel)
    if channel_target:
        _queue_goal(
            target=channel_target,
            client_id=client_id,
            created_at=created_at,
            comment=channel_target,
            context=payload,
            thread_name=f"metrika-{channel_target}",
        )
