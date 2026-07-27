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

    async def delete_messages(self, peer_id: int, message_ids: list[int], *, delete_for_all: bool = True) -> None:
        ids = [int(mid) for mid in message_ids if mid]
        if not ids:
            return
        params: dict[str, Any] = {
            "message_ids": ",".join(str(mid) for mid in ids),
            "delete_for_all": 1 if delete_for_all else 0,
        }
        if self.settings.group_id:
            params["group_id"] = self.settings.group_id
        try:
            await self.api("messages.delete", **params)
            return
        except VKAPIError:
            logger.warning(
                "messages.delete by message_ids failed for peer_id=%s ids=%s, trying cmids",
                peer_id,
                ids,
                exc_info=True,
            )
        # Fallback: conversation message ids are more reliable in community dialogs.
        try:
            history = await self.api("messages.getHistory", peer_id=peer_id, count=30)
        except VKAPIError:
            logger.exception("Failed to load VK history for delete peer_id=%s", peer_id)
            return
        wanted = set(ids)
        cmids: list[int] = []
        for item in history.get("items") or []:
            if int(item.get("id") or 0) in wanted:
                cmid = item.get("conversation_message_id")
                if cmid:
                    cmids.append(int(cmid))
        if not cmids:
            return
        cmid_params: dict[str, Any] = {
            "peer_id": peer_id,
            "cmids": ",".join(str(cid) for cid in cmids),
            "delete_for_all": 1 if delete_for_all else 0,
        }
        if self.settings.group_id:
            cmid_params["group_id"] = self.settings.group_id
        try:
            await self.api("messages.delete", **cmid_params)
        except VKAPIError:
            logger.exception("Failed to delete VK cmids %s for peer_id=%s", cmids, peer_id)

    async def collect_recent_nav_message_ids(
        self,
        peer_id: int,
        *,
        also_ids: list[int] | None = None,
        limit: int = 8,
    ) -> list[int]:
        """Find recent bot/nav messages (and optional button-click ids) to clean the chat."""
        try:
            history = await self.api("messages.getHistory", peer_id=peer_id, count=20)
        except VKAPIError:
            logger.exception("Failed to load VK history for peer_id=%s", peer_id)
            return list(also_ids or [])
        also = {int(mid) for mid in (also_ids or []) if mid}
        found: list[int] = []
        for item in history.get("items") or []:
            mid = int(item.get("id") or 0)
            if not mid:
                continue
            is_out = int(item.get("out") or 0) == 1
            has_keyboard = bool(item.get("keyboard"))
            if mid in also or (is_out and has_keyboard):
                if mid not in found:
                    found.append(mid)
            if len(found) >= limit:
                break
        for mid in also:
            if mid not in found:
                found.append(mid)
        return found

    async def upload_message_photo(self, peer_id: int, image_bytes: bytes, *, filename: str = "photo.jpg") -> str:
        """Upload image bytes and return VK attachment id for messages.send."""
        server = await self.api("photos.getMessagesUploadServer", peer_id=peer_id)
        upload_url = server["upload_url"]
        lower = filename.lower()
        if lower.endswith(".png"):
            content_type = "image/png"
        elif lower.endswith(".webp"):
            content_type = "image/webp"
        else:
            content_type = "image/jpeg"
        form = aiohttp.FormData()
        form.add_field(
            "photo",
            image_bytes,
            filename=filename,
            content_type=content_type,
        )
        async with aiohttp.ClientSession(connector=_connector()) as session:
            async with session.post(upload_url, data=form) as resp:
                uploaded = await resp.json(content_type=None)
        saved = await self.api(
            "photos.saveMessagesPhoto",
            photo=uploaded["photo"],
            server=uploaded["server"],
            hash=uploaded["hash"],
        )
        if not saved:
            raise VKAPIError("VK did not return saved photo")
        photo = saved[0]
        attachment = f"photo{photo['owner_id']}_{photo['id']}"
        if photo.get("access_key"):
            attachment = f"{attachment}_{photo['access_key']}"
        return attachment

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
