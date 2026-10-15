"""Turn the agent's plain-text replies into safe Telegram HTML messages."""

import html
import re

# Telegram's limit is 4096 characters after entity parsing; splitting the raw
# text well below that leaves room for escaping and tags.
CHUNK_CHARS = 3500

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)


def to_telegram_html(text: str) -> str:
    """Escape everything, then restore the one markup the prompt allows."""
    escaped = html.escape(_HEADING.sub("", text.strip()), quote=False)
    return _BOLD.sub(r"<b>\1</b>", escaped)


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
