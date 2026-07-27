import asyncio
import json
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

    async def edit_message(
        self,
        peer_id: int,
        text: str,
        *,
        message_id: int | None = None,
        conversation_message_id: int | None = None,
        keyboard: str | None = None,
        attachment: str | None = None,
    ) -> bool:
        """Edit an existing message in place (text / keyboard / attachment).

        Prefer message_id when known; conversation_message_id works after bot restart
        (comes from message_event on the button that was clicked).
        """
        if not message_id and not conversation_message_id:
            return False
        params: dict[str, Any] = {
            "peer_id": int(peer_id),
            "message": text,
        }
        if message_id:
            params["message_id"] = int(message_id)
        else:
            params["conversation_message_id"] = int(conversation_message_id)
        if self.settings.group_id:
            params["group_id"] = self.settings.group_id
        if keyboard is not None:
            params["keyboard"] = keyboard
        if attachment is not None:
            params["attachment"] = attachment
        try:
            response = await self.api("messages.edit", **params)
            return bool(response)
        except VKAPIError:
            logger.exception(
                "messages.edit failed peer_id=%s message_id=%s cmid=%s",
                peer_id,
                message_id,
                conversation_message_id,
            )
            return False

    async def send_message_event_answer(
        self,
        event_id: str,
        user_id: int,
        peer_id: int,
        *,
        event_data: dict[str, Any] | None = None,
    ) -> None:
        params: dict[str, Any] = {
            "event_id": event_id,
            "user_id": int(user_id),
            "peer_id": int(peer_id),
        }
        if event_data is not None:
            params["event_data"] = json.dumps(event_data, ensure_ascii=False, separators=(",", ":"))
        await self.api("messages.sendMessageEventAnswer", **params)

    async def ensure_long_poll_events(self) -> None:
        """Включает message_new + message_event для Bots Long Poll."""
        if not self.settings.group_id:
            return
        try:
            await self.api(
                "groups.setLongPollSettings",
                group_id=self.settings.group_id,
                enabled=1,
                api_version=self.settings.api_version,
                message_new=1,
                message_event=1,
                message_reply=0,
            )
        except VKAPIError:
            logger.exception("Failed to set VK long poll event types")

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

    async def get_user_display_name(self, user_id: int) -> str:
        response = await self.api("users.get", user_ids=int(user_id))
        if not response:
            return ""
        user = response[0] if isinstance(response, list) else response
        parts = [user.get("first_name") or "", user.get("last_name") or ""]
        return " ".join(p for p in parts if p).strip()

    async def is_group_member(self, user_id: int) -> bool:
        """True if user is a member of VK_GROUP_ID (groups.isMember)."""
        if not self.settings.group_id:
            raise VKAPIError("VK_GROUP_ID is not set")
        response = await self.api(
            "groups.isMember",
            group_id=self.settings.group_id,
            user_id=int(user_id),
        )
        if isinstance(response, dict):
            return bool(int(response.get("member") or 0))
        try:
            return bool(int(response))
        except (TypeError, ValueError):
            return bool(response)

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
