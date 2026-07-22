import asyncio
import logging
import random
import ssl
from typing import Any, AsyncIterator

import aiohttp

from bot.vk.config import VKSettings

logger = logging.getLogger(__name__)


class VKAPIError(RuntimeError):
    pass


def _connector() -> aiohttp.TCPConnector:
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
        return aiohttp.TCPConnector(ssl=context)
    except Exception:
        return aiohttp.TCPConnector()


class VKClient:
    def __init__(self, settings: VKSettings):
        self.settings = settings
        self.api_url = "https://api.vk.com/method"

    async def api(self, method: str, **params) -> dict[str, Any]:
        payload = {
            "access_token": self.settings.group_token,
            "v": self.settings.api_version,
            **params,
        }
        async with aiohttp.ClientSession(connector=_connector()) as session:
            async with session.post(f"{self.api_url}/{method}", data=payload) as resp:
                data = await resp.json(content_type=None)
        if "error" in data:
            error = data["error"]
            raise VKAPIError(f"{method}: {error.get('error_msg') or error}")
        return data.get("response", {})

    async def send_message(
        self,
        peer_id: int,
        text: str,
        *,
        keyboard: str | None = None,
        attachment: str | None = None,
    ) -> int:
        params: dict[str, Any] = {
            "peer_id": peer_id,
            "random_id": random.randint(1, 2_147_483_647),
            "message": text,
        }
        if keyboard:
            params["keyboard"] = keyboard
        if attachment:
            params["attachment"] = attachment
        response = await self.api("messages.send", **params)
        return int(response)

    async def get_long_poll_server(self) -> dict[str, Any]:
        if not self.settings.group_id:
            raise VKAPIError("VK_GROUP_ID is not set")
        return await self.api("groups.getLongPollServer", group_id=self.settings.group_id)

    async def long_poll(self) -> AsyncIterator[dict[str, Any]]:
        server = await self.get_long_poll_server()
        ts = server["ts"]
        async with aiohttp.ClientSession(connector=_connector()) as session:
            while True:
                try:
                    async with session.get(
                        server["server"],
                        params={
                            "act": "a_check",
                            "key": server["key"],
                            "ts": ts,
                            "wait": 25,
                            "mode": 2,
                            "version": 3,
                        },
                        timeout=35,
                    ) as resp:
                        data = await resp.json(content_type=None)
                    if "failed" in data:
                        logger.warning("VK long poll failed: %s", data)
                        server = await self.get_long_poll_server()
                        ts = server["ts"]
                        continue
                    ts = data.get("ts", ts)
                    for update in data.get("updates", []):
                        yield update
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    logger.debug("VK long poll timeout, reconnecting")
                    continue
                except Exception:
                    logger.exception("VK long poll iteration failed")
                    await asyncio.sleep(3)
