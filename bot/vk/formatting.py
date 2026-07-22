import html
import re


_BR_RE = re.compile(r"<\s*br\s*/?\s*>", flags=re.IGNORECASE)
_PARAGRAPH_RE = re.compile(r"</\s*(p|div|li|h[1-6])\s*>", flags=re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_A_RE = re.compile(
    r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    flags=re.IGNORECASE | re.DOTALL,
)


def _link_repl(match: re.Match) -> str:
    url = html.unescape(match.group(1)).strip()
    label = format_vk_text(match.group(2)).strip()
    if not label or label == url:
        return url
    return f"{label}: {url}"


def format_vk_text(text: str | None) -> str:
    """Convert Telegram-style HTML text to a VK-safe plain message.

    VK messages do not support Telegram parse_mode HTML. We keep the readable
    content, preserve links, and strip formatting tags such as <b>/<i>/<u>.
    """
    if not text:
        return ""
    value = str(text)
    value = _A_RE.sub(_link_repl, value)
    value = _BR_RE.sub("\n", value)
    value = _PARAGRAPH_RE.sub("\n", value)
    value = _TAG_RE.sub("", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


# Alias named after the wording from VK migration notes.
format_data = format_vk_text
