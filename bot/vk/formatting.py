import html
import json
import re
from typing import Any


_BR_RE = re.compile(r"<\s*br\s*/?\s*>", flags=re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(r"</\s*(p|div|li|h[1-6]|blockquote)\s*>", flags=re.IGNORECASE)
_BLOCK_OPEN_RE = re.compile(r"<\s*(p|div|li|h[1-6]|blockquote)(?:\s[^>]*)?>", flags=re.IGNORECASE)
_A_RE = re.compile(
    r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    flags=re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(
    r"(?P<br><\s*br\s*/?\s*>)"
    r"|(?P<a_open><\s*a\b[^>]*href\s*=\s*[\"'](?P<href>[^\"']+)[\"'][^>]*>)"
    r"|(?P<a_close></\s*a\s*>)"
    r"|(?P<b_open><\s*(?:b|strong)\s*>)"
    r"|(?P<b_close></\s*(?:b|strong)\s*>)"
    r"|(?P<i_open><\s*(?:i|em)\s*>)"
    r"|(?P<i_close></\s*(?:i|em)\s*>)"
    r"|(?P<u_open><\s*u\s*>)"
    r"|(?P<u_close></\s*u\s*>)"
    r"|(?P<h_open><\s*h[1-6]\s*>)"
    r"|(?P<h_close></\s*h[1-6]\s*>)"
    r"|(?P<other><[^>]+>)",
    flags=re.IGNORECASE,
)


def utf16_len(value: str) -> int:
    """VK format_data offsets are in UTF-16 code units."""
    return len(value.encode("utf-16-le")) // 2


def _strip_tags_plain(text: str) -> str:
    value = _BR_RE.sub("\n", text)
    value = _BLOCK_CLOSE_RE.sub("\n", value)
    value = _BLOCK_OPEN_RE.sub("", value)
    value = _TAG_RE.sub("", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _link_repl_plain(match: re.Match) -> str:
    url = html.unescape(match.group(1)).strip()
    label = _strip_tags_plain(match.group(2))
    if not label or label == url:
        return url
    return f"{label}: {url}"


def format_vk_text(text: str | None) -> str:
    """Convert Telegram-style HTML to plain text (no VK formatting)."""
    if not text:
        return ""
    value = _A_RE.sub(_link_repl_plain, str(text))
    return _strip_tags_plain(value)


def prepare_vk_message(text: str | None) -> tuple[str, str | None]:
    """Convert HTML subset to VK message text + optional format_data JSON.

    Supports <b>/<strong>, <i>/<em>, <u>, <a href>, headings as bold.
    Offsets use UTF-16 units as required by VK.
    """
    if not text:
        return "", None
    if "<" not in str(text):
        return str(text), None

    value = str(text)
    value = _BR_RE.sub("\n", value)
    value = _BLOCK_CLOSE_RE.sub("\n", value)
    value = _BLOCK_OPEN_RE.sub("", value)

    plain_parts: list[str] = []
    items: list[dict[str, Any]] = []
    stack: list[tuple[str, int, str | None]] = []
    pos = 0
    utf16_pos = 0

    def _append(chunk: str) -> None:
        nonlocal utf16_pos
        if not chunk:
            return
        chunk = html.unescape(chunk)
        plain_parts.append(chunk)
        utf16_pos += utf16_len(chunk)

    def _close(kind: str) -> None:
        for i in range(len(stack) - 1, -1, -1):
            if stack[i][0] != kind:
                continue
            _, start, url = stack.pop(i)
            length = utf16_pos - start
            if length <= 0:
                return
            item: dict[str, Any] = {"type": kind, "offset": start, "length": length}
            if kind == "url" and url:
                item["url"] = url
            items.append(item)
            return

    for match in _TOKEN_RE.finditer(value):
        _append(value[pos : match.start()])
        pos = match.end()
        if match.group("br"):
            _append("\n")
        elif match.group("a_open"):
            stack.append(("url", utf16_pos, html.unescape(match.group("href") or "").strip()))
        elif match.group("a_close"):
            _close("url")
        elif match.group("b_open") or match.group("h_open"):
            stack.append(("bold", utf16_pos, None))
        elif match.group("b_close") or match.group("h_close"):
            _close("bold")
        elif match.group("i_open"):
            stack.append(("italic", utf16_pos, None))
        elif match.group("i_close"):
            _close("italic")
        elif match.group("u_open"):
            stack.append(("underline", utf16_pos, None))
        elif match.group("u_close"):
            _close("underline")
        # else: drop unknown tags

    _append(value[pos:])

    raw_joined = "".join(plain_parts)
    plain = re.sub(r"[ \t]+\n", "\n", raw_joined)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    # Compute leading trim in utf16 before strip
    stripped = plain.strip()
    if not stripped:
        return "", None
    lead_chars = len(plain) - len(plain.lstrip(" \t\n"))
    lead_utf16 = utf16_len(plain[:lead_chars]) if lead_chars else 0
    plain = stripped

    if not items:
        return plain, None

    adjusted: list[dict[str, Any]] = []
    plain_utf16 = utf16_len(plain)
    for item in items:
        start = int(item["offset"]) - lead_utf16
        length = int(item["length"])
        if start < 0:
            length += start
            start = 0
        if length <= 0 or start >= plain_utf16:
            continue
        length = min(length, plain_utf16 - start)
        new_item = {"type": item["type"], "offset": start, "length": length}
        if item["type"] == "url" and item.get("url"):
            new_item["url"] = item["url"]
        adjusted.append(new_item)

    if not adjusted:
        return plain, None

    payload = {"version": "1", "items": adjusted}
    return plain, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# Alias named after the wording from VK migration notes.
format_data = format_vk_text
