"""Read-only access to a household member's own Gmail inbox.

Separate from the assistant inbox (gmail.py): each person signs in once with
``google_auth --account <address> --readonly``, which stores a token limited
to the ``gmail.readonly`` scope, so Google itself prevents sending, deleting or
relabelling anything here.
"""

import base64

from django.conf import settings
from googleapiclient.discovery import build

from .email_parser import parse_email_message
from .gmail import get_google_credentials

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MAX_BODY_CHARS = 8000


def personal_address(role: str) -> str:
    """The configured personal Gmail address for a household role, or ""."""
    addresses = {"ike": settings.PERSONAL_GMAIL_IKE, "wife": settings.PERSONAL_GMAIL_WIFE}
    return (addresses.get(role) or "").strip().lower()


def _service(address: str):
    return build("gmail", "v1", credentials=get_google_credentials(address), cache_discovery=False)


def search_messages(address: str, query: str, *, max_results: int = 10) -> list[dict]:
    """Newest-first metadata for messages matching a Gmail search query."""
    service = _service(address)
    listed = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute().get("messages", [])
    results = []
    for item in listed[:max_results]:
        message = service.users().messages().get(
            userId="me", id=item["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
        headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
        results.append({
            "message_id": message["id"],
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "snippet": message.get("snippet", ""),
            "unread": "UNREAD" in message.get("labelIds", []),
        })
    return results


def read_message(address: str, message_id: str) -> dict:
    """One message's headers and readable body text (truncated)."""
    response = _service(address).users().messages().get(
        userId="me", id=message_id, format="raw"
    ).execute()
    parsed = parse_email_message(base64.urlsafe_b64decode(response["raw"].encode("utf-8")))
    body = parsed.cleaned_text or parsed.body_text
    return {
        "message_id": message_id,
        "from": parsed.from_addr,
        "to": ", ".join(parsed.to_addrs),
        "subject": parsed.subject,
        "date": parsed.date,
        "body": body[:MAX_BODY_CHARS],
        "truncated": len(body) > MAX_BODY_CHARS,
        "attachments": [a.get("filename", "attachment") for a in parsed.attachments],
    }
