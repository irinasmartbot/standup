import json
from dataclasses import dataclass
from typing import Any, Literal


VK_MAX_BUTTONS = 10
VK_MAX_ROWS = 6
VK_MAX_BUTTONS_PER_ROW = 4
VK_MAX_COLUMNS = 6
VK_MAX_PAYLOAD_BYTES = 255

ButtonColor = Literal["primary", "secondary", "negative", "positive"]


@dataclass(frozen=True)
class VKButton:
    label: str
    payload: dict[str, Any] | None = None
    link: str | None = None
    color: ButtonColor = "secondary"

    def as_vk(self) -> dict[str, Any]:
        if self.link:
            return {
                "action": {
                    "type": "open_link",
                    "label": self.label,
                    "link": self.link,
                }
            }

        payload = json.dumps(self.payload or {}, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > VK_MAX_PAYLOAD_BYTES:
            raise ValueError(f"VK button payload is too long: {self.label}")
        return {
            "action": {
                "type": "text",
                "label": self.label,
                "payload": payload,
            },
            "color": self.color,
        }


class VKKeyboardBuilder:
    """Small VK keyboard builder with conservative VK limits.

    The limits intentionally match the migration constraints we want to enforce
    from day one: max 10 buttons total, max 4 buttons per row, max 6 rows.
    """

    def __init__(self, one_time: bool = False, inline: bool = False):
        self.one_time = one_time
        self.inline = inline
        self._rows: list[list[VKButton]] = [[]]

    def button(
        self,
        label: str,
        payload: dict[str, Any] | None = None,
        *,
        link: str | None = None,
        color: ButtonColor = "secondary",
    ) -> "VKKeyboardBuilder":
        if self.total_buttons >= VK_MAX_BUTTONS:
            raise ValueError("VK keyboard cannot contain more than 10 buttons")
        if len(self._rows[-1]) >= VK_MAX_BUTTONS_PER_ROW:
            self.row()
        self._rows[-1].append(VKButton(label=label, payload=payload, link=link, color=color))
        return self

    def row(self) -> "VKKeyboardBuilder":
        if len(self._rows) >= min(VK_MAX_ROWS, VK_MAX_COLUMNS):
            raise ValueError("VK keyboard cannot contain more than 6 rows")
        if self._rows[-1]:
            self._rows.append([])
        return self

    def adjust(self, *widths: int) -> "VKKeyboardBuilder":
        buttons = [button for row in self._rows for button in row]
        if not buttons:
            return self
        if not widths:
            widths = (VK_MAX_BUTTONS_PER_ROW,)
        rows: list[list[VKButton]] = []
        index = 0
        width_index = 0
        for width in widths:
            if index >= len(buttons):
                break
            width = max(1, min(width, VK_MAX_BUTTONS_PER_ROW))
            rows.append(buttons[index : index + width])
            index += width
            width_index += 1
        last_width = max(1, min(widths[-1], VK_MAX_BUTTONS_PER_ROW))
        while index < len(buttons):
            rows.append(buttons[index : index + last_width])
            index += last_width
        if len(rows) > min(VK_MAX_ROWS, VK_MAX_COLUMNS):
            raise ValueError("VK keyboard cannot contain more than 6 rows")
        self._rows = rows
        return self

    @property
    def total_buttons(self) -> int:
        return sum(len(row) for row in self._rows)

    def as_json(self) -> str:
        rows = [row for row in self._rows if row]
        if len(rows) > min(VK_MAX_ROWS, VK_MAX_COLUMNS):
            raise ValueError("VK keyboard cannot contain more than 6 rows")
        return json.dumps(
            {
                "one_time": self.one_time,
                "inline": self.inline,
                "buttons": [[button.as_vk() for button in row] for row in rows],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def empty_keyboard() -> str:
    return json.dumps({"buttons": [], "one_time": True}, separators=(",", ":"))
