"""Merge local VK_* keys into remote /home/standup/vk-app/.env over SSH.

Usage (from project root, on your PC):
  python scripts/sync_vk_env_to_server.py

Does not print token values.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

VK_KEYS = (
    "VK_ENABLED",
    "VK_GROUP_ID",
    "VK_GROUP_TOKEN",
    "VK_API_VERSION",
    "VK_ADMIN_PEER_ID",
    "VK_MANAGER_LINK",
    "VK_COMMUNITY_LINK",
    "VK_SYSTEM_IMAGES_CACHE",
)

REMOTE_ENV = "/home/standup/vk-app/.env"


def _load_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        raise SystemExit(f"Local env not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env", help="Local env file")
    parser.add_argument("--host", default="standup@31.128.47.4")
    parser.add_argument("--remote-env", default=REMOTE_ENV)
    args = parser.parse_args()

    local = _load_env(Path(args.env))
    payload: dict[str, str] = {}
    missing = []
    for key in VK_KEYS:
        value = (local.get(key) or "").strip()
        if not value:
            missing.append(key)
            continue
        payload[key] = value

    if "VK_GROUP_TOKEN" in missing or "VK_GROUP_ID" in missing:
        raise SystemExit(
            "Local .env is missing required VK_GROUP_ID / VK_GROUP_TOKEN. "
            f"Missing: {', '.join(missing)}"
        )

    payload.setdefault("VK_ENABLED", "1")
    payload.setdefault("VK_API_VERSION", "5.199")
    payload.setdefault("VK_SYSTEM_IMAGES_CACHE", "data/storage/vk_system_images.json")

    print("Will sync keys:")
    for key in VK_KEYS:
        if key not in payload:
            print(f"  {key}: SKIP (empty locally)")
            continue
        if "TOKEN" in key:
            print(f"  {key}: SET (len={len(payload[key])})")
        else:
            print(f"  {key}: {payload[key]}")

    b64_payload = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    remote_cmd = (
        "python3 - <<'PY'\n"
        "import base64, json, pathlib\n"
        f"path = pathlib.Path({args.remote_env!r})\n"
        f"incoming = json.loads(base64.b64decode({b64_payload!r}).decode('utf-8'))\n"
        "lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []\n"
        "out, seen = [], set()\n"
        "for line in lines:\n"
        "    key = line.split('=', 1)[0] if ('=' in line and not line.lstrip().startswith('#')) else None\n"
        "    if key in incoming:\n"
        "        out.append(f'{key}={incoming[key]}')\n"
        "        seen.add(key)\n"
        "    else:\n"
        "        out.append(line)\n"
        "for key, value in incoming.items():\n"
        "    if key not in seen:\n"
        "        out.append(f'{key}={value}')\n"
        "path.write_text('\\n'.join(out) + '\\n', encoding='utf-8')\n"
        "print('REMOTE_ENV_UPDATED', path)\n"
        "print('VK_KEYS', ','.join(sorted(incoming)))\n"
        "PY"
    )
    result = subprocess.run(["ssh", args.host, remote_cmd], text=True)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print("Done. Next on server: upload images + systemctl enable --now standup-vk-bot")


if __name__ == "__main__":
    main()
