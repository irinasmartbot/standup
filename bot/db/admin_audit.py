"""Admin action audit log (who / what / when)."""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

from bot.config import BOOKINGS_SOURCE, DATABASE_URL

logger = logging.getLogger(__name__)


def _use_postgres() -> bool:
    return BOOKINGS_SOURCE == "postgres" and bool(DATABASE_URL)


def ensure_admin_audit_table() -> None:
    if not _use_postgres():
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS admin_audit_log (
                        id BIGSERIAL PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        actor_role TEXT NOT NULL DEFAULT '',
                        action TEXT NOT NULL,
                        entity_type TEXT NOT NULL DEFAULT '',
                        entity_id TEXT NOT NULL DEFAULT '',
                        details JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_admin_audit_created
                    ON admin_audit_log (created_at DESC)
                    """
                )
            conn.commit()
    except Exception:
        logger.exception("ensure_admin_audit_table failed")


def log_admin_action(
    *,
    actor_role: str,
    action: str,
    entity_type: str = "",
    entity_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    if not _use_postgres():
        return
    try:
        ensure_admin_audit_table()
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO admin_audit_log (actor_role, action, entity_type, entity_id, details)
                    VALUES (%(actor_role)s, %(action)s, %(entity_type)s, %(entity_id)s, %(details)s::jsonb)
                    """,
                    {
                        "actor_role": (actor_role or "")[:64],
                        "action": (action or "")[:128],
                        "entity_type": (entity_type or "")[:64],
                        "entity_id": str(entity_id or "")[:64],
                        "details": json.dumps(details or {}, ensure_ascii=False),
                    },
                )
            conn.commit()
    except Exception:
        logger.exception("log_admin_action failed: %s", action)


def fetch_admin_audit(limit: int = 100) -> list[dict]:
    if not _use_postgres():
        return []
    try:
        ensure_admin_audit_table()
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, created_at, actor_role, action, entity_type, entity_id, details
                    FROM admin_audit_log
                    ORDER BY created_at DESC, id DESC
                    LIMIT %(limit)s
                    """,
                    {"limit": max(1, min(int(limit or 100), 500))},
                )
                return [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception("fetch_admin_audit failed")
        return []
