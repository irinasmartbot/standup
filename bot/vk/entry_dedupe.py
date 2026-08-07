"""Cross-process dedupe for VK entry messages (mini app / landing / bot).

In-memory cooldown alone is not enough: standup-admin may run multiple workers,
and the bot process does not share that dict. Without a shared lock the same
user can get «Привет-привет» several times in one second.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SEC = 45.0
_STATE_DIR = Path(
    os.getenv("VK_ENTRY_DEDUPE_DIR", "/tmp/standup_vk_entry_dedupe")
)
_memory: dict[str, float] = {}


def _key(vk_id: int, flow: str) -> str:
    return f"{int(vk_id)}:{str(flow).strip()}"


def _path_for(key: str) -> Path:
    safe = key.replace("/", "_").replace("\\", "_")
    return _STATE_DIR / f"{safe}.ts"


def claim_flow_send(
    vk_id: int,
    flow: str,
    *,
    ttl_sec: float = _DEFAULT_TTL_SEC,
) -> bool:
    """Return True if this caller should send; False if a recent send already claimed."""
    if vk_id <= 0 or not flow:
        return True
    key = _key(vk_id, flow)
    now = time.time()
    mem = _memory.get(key)
    if mem is not None and (now - mem) < ttl_sec:
        return False

    path = _path_for(key)
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Exclusive create: first writer wins within ttl window.
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(now).encode("ascii"))
            finally:
                os.close(fd)
            _memory[key] = now
            return True
        except FileExistsError:
            try:
                age = now - path.stat().st_mtime
            except OSError:
                age = 0.0
            if age < ttl_sec:
                _memory[key] = now - age
                return False
            # Stale claim — overwrite.
            path.write_text(str(now), encoding="ascii")
            _memory[key] = now
            return True
    except OSError as exc:
        logger.warning("VK entry dedupe fallback to memory: %s", exc)
        if mem is not None and (now - mem) < ttl_sec:
            return False
        _memory[key] = now
        return True


def clear_flow_send(vk_id: int, flow: str) -> None:
    """Allow retry after a failed send."""
    if vk_id <= 0 or not flow:
        return
    key = _key(vk_id, flow)
    _memory.pop(key, None)
    try:
        _path_for(key).unlink(missing_ok=True)
    except OSError:
        pass
