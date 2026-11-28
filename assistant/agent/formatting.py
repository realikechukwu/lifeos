"""Turn the agent's plain-text replies into safe Telegram HTML messages."""

import html
import re

# Telegram's limit is 4096 characters after entity parsing; splitting the raw
# text well below that leaves room for escaping and tags.
CHUNK_CHARS = 3500

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_INLINE = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)


def to_telegram_html(text: str) -> str:
    """Escape everything, then restore the one markup the prompt allows."""
    escaped = html.escape(_HEADING.sub("", text.strip()), quote=False)
    return _BOLD.sub(r"<b>\1</b>", escaped)


def _format_web_inline(line: str) -> str:
    escaped = html.escape(line, quote=True)
    return _BOLD_INLINE.sub(r"<strong>\1</strong>", escaped)


def to_web_html(text: str) -> str:
    """Escape assistant text for the web UI, supporting only **bold** and '• ' lists."""
    cleaned = (text or "").replace("\r\n", "\n").strip()
    if not cleaned:
        return ""
    blocks: list[str] = []
    para_lines: list[str] = []
    list_items: list[str] = []

    def flush_para():
        if para_lines:
            blocks.append("<p>" + "<br>".join(_format_web_inline(line) for line in para_lines) + "</p>")
            para_lines.clear()

    def flush_list():
        if list_items:
            blocks.append(
                '<ul class="ask-list">'
                + "".join(f"<li>{_format_web_inline(item)}</li>" for item in list_items)
                + "</ul>"
            )
            list_items.clear()

    for raw_line in cleaned.split("\n"):
        line = raw_line.strip()
        if not line:
            flush_para()
            flush_list()
        elif line.startswith("• "):
            flush_para()
            list_items.append(line[2:].strip())
        else:
            flush_list()
            para_lines.append(_HEADING.sub("", line))
    flush_para()
    flush_list()
    return "".join(blocks)


def split_message(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split on paragraph, then line, then hard boundaries to fit Telegram."""
    text = text.strip()
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks
