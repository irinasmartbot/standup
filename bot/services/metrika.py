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


def queue_booking_created_goal(
    *,
    client_id: str,
    created_at: datetime | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Send booking_created in a daemon thread so users do not wait for Metrica."""
    target = _booking_created_target()
    thread = threading.Thread(
        target=upload_offline_conversion,
        kwargs={
            "target": target,
            "client_id": client_id,
            "created_at": created_at,
            "comment": "booking_created",
            "context": context or {},
        },
        name="metrika-booking-created",
        daemon=True,
    )
    thread.start()
