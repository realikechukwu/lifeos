"""Gmail API access: reading messages, managing labels, sending threaded replies."""

import base64
from email.mime.text import MIMEText

from django.conf import settings
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from core.models import GoogleCredential, HouseholdMember, normalise_email


def is_authorised_sender(email_address: str) -> bool:
    """The authorised sender is the *outer* sender who forwarded/sent the
    email — never an address found inside the forwarded content or body."""
    address = normalise_email(email_address)
    if not address:
        return False

    configured = {normalise_email(settings.AUTHORISED_EMAIL_IKE), normalise_email(settings.AUTHORISED_EMAIL_WIFE)}
    configured.discard("")
    if address in configured:
        return True

    return HouseholdMember.objects.filter(email=address, authorised=True, active=True).exists()


def get_google_credentials(account_email: str | None = None) -> Credentials:
    """Load (and refresh if needed) stored OAuth credentials for the assistant
    account. Shared by gmail.py and google_calendar.py so there is exactly one
    place that reads/writes the encrypted GoogleCredential row."""
    account_email = account_email or settings.GOOGLE_ASSISTANT_EMAIL
    try:
        record = GoogleCredential.objects.get(account_email=account_email)
    except GoogleCredential.DoesNotExist as exc:
        raise RuntimeError(
            f"No Google credentials stored for {account_email}. "
            "Run `python manage.py google_auth` first."
        ) from exc

    data = record.get_credentials()
    if not data:
        raise RuntimeError(f"Stored Google credentials for {account_email} could not be decrypted.")

    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id") or settings.GOOGLE_CLIENT_ID,
        client_secret=data.get("client_secret") or settings.GOOGLE_CLIENT_SECRET,
        scopes=data.get("scopes"),
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        record.set_credentials({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes) if creds.scopes else [],
        })
        record.save(update_fields=["encrypted_data", "updated_at"])

    return creds


class GmailService:
    def __init__(self, credentials=None):
        self.credentials = credentials or get_google_credentials()
        self._service = build("gmail", "v1", credentials=self.credentials, cache_discovery=False)

    def list_message_ids(self, query: str, max_results: int) -> list[str]:
        response = self._service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        return [m["id"] for m in response.get("messages", [])][:max_results]

    def get_message_raw(self, message_id: str) -> tuple[bytes, str]:
        """Returns (raw_rfc822_bytes, thread_id) for the complete MIME message."""
        response = self._service.users().messages().get(
            userId="me", id=message_id, format="raw"
        ).execute()
        raw_bytes = base64.urlsafe_b64decode(response["raw"].encode("utf-8"))
        return raw_bytes, response.get("threadId", "")

    def ensure_labels(self, label_names: list[str]) -> dict[str, str]:
        """Create any missing labels and return a name -> label id map."""
        existing = self._service.users().labels().list(userId="me").execute().get("labels", [])
        name_to_id = {lbl["name"]: lbl["id"] for lbl in existing}
        for name in label_names:
            if name not in name_to_id:
                created = self._service.users().labels().create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                ).execute()
                name_to_id[name] = created["id"]
        return name_to_id

    def apply_label(self, message_id: str, label_id: str) -> None:
        self._service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [label_id]}
        ).execute()

    def send_reply(
        self,
        *,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
        in_reply_to_message_id_header: str | None = None,
        cc_addr: str | None = None,
    ) -> str:
        """Send a plain-text reply, threaded where a thread id / Message-ID header
        is available. Returns the sent message's Gmail id. `to_addr` is always
        the authorised recipient; `cc_addr` (comma-separated for more than
        one) is optional and additive only."""
        message = MIMEText(body_text)
        message["To"] = to_addr
        if cc_addr:
            message["Cc"] = cc_addr
        message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if in_reply_to_message_id_header:
            message["In-Reply-To"] = in_reply_to_message_id_header
            message["References"] = in_reply_to_message_id_header

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        body = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id

        sent = self._service.users().messages().send(userId="me", body=body).execute()
        return sent.get("id", "")

    def send_message(
        self,
        *,
        to_addr: str,
        subject: str,
        body_text: str,
        message_id_header: str | None = None,
    ) -> str:
        """Send a fresh (non-reply) plain-text message — used for reminder
        notifications, which are not part of any existing thread. `to_addr`
        may be a comma-separated list for a "both" recipient."""
        message = MIMEText(body_text)
        message["To"] = to_addr
        message["Subject"] = subject
        if message_id_header:
            message["Message-ID"] = message_id_header

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        sent = self._service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return sent.get("id", "")
