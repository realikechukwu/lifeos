"""Parse a raw RFC822 Gmail message into plain text, detect forwarded content,
and record attachment metadata. Attachments are never downloaded.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from zoneinfo import ZoneInfo

import bleach
from bs4 import BeautifulSoup
from django.conf import settings

from core.models import normalise_email

ALLOWED_HTML_TAGS = [
    "p", "br", "div", "span", "table", "thead", "tbody", "tr", "td", "th",
    "a", "b", "strong", "i", "em", "ul", "ol", "li", "blockquote",
]
ALLOWED_HTML_ATTRS = {"a": ["href", "title"]}

FORWARD_MARKER_RE = re.compile(
    r"(-{2,}\s*Forwarded message\s*-{2,}|Begin forwarded message\s*:|-{2,}\s*Original Message\s*-{2,})",
    re.IGNORECASE,
)
REPLY_MARKER_RE = re.compile(r"^On .{0,150}?wrote:\s*$", re.IGNORECASE | re.MULTILINE)
HEADER_LINE_RE = re.compile(r"^(From|Date|Sent|Subject|To|Cc)\s*:\s*(.*)$", re.IGNORECASE)

_FALLBACK_DATE_FORMATS = (
    "%d %B %Y at %H:%M",
    "%d %B %Y, %H:%M",
    "%A, %d %B %Y %H:%M",
    "%d/%m/%Y %H:%M",
    "%d %B %Y",
)


@dataclass
class ParsedEmail:
    subject: str = ""
    from_addr: str = ""
    to_addrs: list = field(default_factory=list)
    date: datetime | None = None

    body_text: str = ""
    body_html_raw: str = ""
    body_html_sanitised: str = ""
    cleaned_text: str = ""

    attachments: list = field(default_factory=list)
    has_attachments: bool = False

    new_instruction: str = ""
    forwarded_body: str = ""
    forwarded_headers_text: str = ""
    is_forwarded: bool = False
    original_forwarded_sender: str = ""
    original_forwarded_date: datetime | None = None


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(settings.APP_TIMEZONE))
    return dt


def _parse_date_string(date_str: str) -> datetime | None:
    date_str = date_str.strip()
    if not date_str:
        return None
    try:
        return _ensure_aware(parsedate_to_datetime(date_str))
    except (TypeError, ValueError):
        pass
    for fmt in _FALLBACK_DATE_FORMATS:
        try:
            return _ensure_aware(datetime.strptime(date_str, fmt))
        except ValueError:
            continue
    return None


def sanitise_html(html: str) -> str:
    return bleach.clean(html, tags=ALLOWED_HTML_TAGS, attributes=ALLOWED_HTML_ATTRS, strip=True)


def html_to_text(html: str) -> str:
    """Render sanitised HTML as readable text, preserving table rows as `cell | cell`."""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = []
        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if cells:
                rows.append(" | ".join(cells))
        table.replace_with("\n" + "\n".join(rows) + "\n")
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def split_new_instruction_and_forward(text: str) -> tuple[str, str, str]:
    """Split cleaned body text into (new_instruction, forwarded_body, forwarded_headers_text)."""
    if not text:
        return "", "", ""

    match = FORWARD_MARKER_RE.search(text)
    if not match:
        match = REPLY_MARKER_RE.search(text)
    if not match:
        return text.strip(), "", ""

    new_instruction = text[: match.start()].strip()
    remainder_lines = text[match.start():].splitlines()

    header_lines = []
    body_start_idx = 1
    for idx, line in enumerate(remainder_lines[1:], start=1):
        stripped = line.strip()
        if HEADER_LINE_RE.match(stripped):
            header_lines.append(stripped)
            continue
        if header_lines and stripped == "":
            body_start_idx = idx + 1
            break
        if header_lines and stripped != "":
            body_start_idx = idx
            break
        if not header_lines and stripped == "":
            continue

    if not header_lines:
        # e.g. "On 3 August 2026, Jane <jane@x.com> wrote:" — keep the marker line
        # itself as the "header" text so a date can still be extracted from it.
        header_lines = [remainder_lines[0].strip()]

    forwarded_headers_text = "\n".join(header_lines)
    forwarded_body = "\n".join(remainder_lines[body_start_idx:]).strip()
    return new_instruction, forwarded_body, forwarded_headers_text


def extract_forwarded_sender(header_text: str) -> str:
    m = re.search(r"From\s*:\s*(.+)", header_text, re.IGNORECASE)
    if not m:
        return ""
    _, addr = parseaddr(m.group(1).strip())
    return normalise_email(addr) if addr else ""


def extract_forwarded_date(header_text: str) -> datetime | None:
    if not header_text:
        return None
    m = re.search(r"(?:Date|Sent)\s*:\s*(.+)", header_text, re.IGNORECASE)
    if m:
        parsed = _parse_date_string(m.group(1))
        if parsed:
            return parsed
    # "On <date>, <name> wrote:" style
    m2 = re.search(r"On\s+(.+?),?\s+[^,]*wrote:", header_text, re.IGNORECASE)
    if m2:
        return _parse_date_string(m2.group(1))
    return None


def parse_email_message(raw_bytes: bytes) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)

    subject = str(msg.get("Subject", "") or "")
    _, from_addr = parseaddr(str(msg.get("From", "") or ""))
    to_addrs = [addr for _, addr in getaddresses([str(msg.get("To", "") or "")]) if addr]

    date_header = msg.get("Date")
    date = _parse_date_string(str(date_header)) if date_header else None

    body_text = ""
    body_html_raw = ""

    plain_part = msg.get_body(preferencelist=("plain",))
    if plain_part is not None:
        try:
            body_text = plain_part.get_content()
        except Exception:
            body_text = ""

    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        try:
            body_html_raw = html_part.get_content()
        except Exception:
            body_html_raw = ""

    body_html_sanitised = sanitise_html(body_html_raw) if body_html_raw else ""

    if body_text.strip():
        cleaned_text = body_text.strip()
    elif body_html_sanitised:
        cleaned_text = html_to_text(body_html_sanitised)
    else:
        cleaned_text = ""

    attachments = []
    try:
        for part in msg.iter_attachments():
            attachments.append({
                "filename": part.get_filename() or "attachment",
                "mime_type": part.get_content_type(),
            })
    except Exception:
        pass

    parsed = ParsedEmail(
        subject=subject,
        from_addr=normalise_email(from_addr),
        to_addrs=[normalise_email(a) for a in to_addrs],
        date=date,
        body_text=body_text,
        body_html_raw=body_html_raw,
        body_html_sanitised=body_html_sanitised,
        cleaned_text=cleaned_text,
        attachments=attachments,
        has_attachments=bool(attachments),
    )

    new_instruction, forwarded_body, forwarded_headers_text = split_new_instruction_and_forward(cleaned_text)
    parsed.new_instruction = new_instruction
    parsed.forwarded_body = forwarded_body
    parsed.forwarded_headers_text = forwarded_headers_text
    parsed.is_forwarded = bool(forwarded_body) or bool(FORWARD_MARKER_RE.search(cleaned_text or ""))

    if forwarded_headers_text:
        parsed.original_forwarded_sender = extract_forwarded_sender(forwarded_headers_text)
        parsed.original_forwarded_date = extract_forwarded_date(forwarded_headers_text)

    return parsed
