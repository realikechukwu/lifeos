"""python manage.py google_auth [--account <address> --readonly]

Runs the local browser-based OAuth consent flow and stores the resulting
credentials, Fernet-encrypted, in GoogleCredential. Intended to be run on a
machine with a browser (your local computer) — not on the Oracle server.

With no options it authorises the assistant Gmail / Calendar account. With
``--account <address> --readonly`` it connects a household member's personal
Gmail for read-only search by the agent (gmail.readonly scope only).
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from core.models import GoogleCredential
from assistant.services.personal_inbox import READONLY_SCOPE

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
]


class Command(BaseCommand):
    help = "Authorise the assistant Google account (or a personal inbox, read-only) via a local browser OAuth flow."

    def add_arguments(self, parser):
        parser.add_argument("--account", help="Personal Gmail address to connect (requires --readonly).")
        parser.add_argument("--readonly", action="store_true", help="Request gmail.readonly only.")

    def handle(self, *args, **options):
        if bool(options["account"]) != options["readonly"]:
            raise CommandError("Use --account and --readonly together to connect a personal inbox.")
        scopes = [READONLY_SCOPE] if options["readonly"] else SCOPES
        if settings.GOOGLE_CLIENT_SECRETS_FILE:
            flow = InstalledAppFlow.from_client_secrets_file(
                settings.GOOGLE_CLIENT_SECRETS_FILE, scopes=scopes
            )
        elif settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET:
            client_config = {
                "installed": {
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
        else:
            raise CommandError(
                "Set GOOGLE_CLIENT_SECRETS_FILE, or both GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET, in your environment before running google_auth."
            )

        self.stdout.write("Opening your browser to sign in to Google...")
        credentials = flow.run_local_server(port=0)

        account_email = settings.GOOGLE_ASSISTANT_EMAIL
        if options["readonly"]:
            account_email = options["account"].strip().lower()
            # The browser may already be signed in to a different Google
            # account; never store one person's token under another address.
            profile = build("gmail", "v1", credentials=credentials, cache_discovery=False)
            signed_in = profile.users().getProfile(userId="me").execute().get("emailAddress", "").lower()
            if signed_in != account_email:
                raise CommandError(
                    f"You signed in as {signed_in or 'an unknown account'}, not {account_email}. "
                    "Nothing was stored; run the command again and pick the right account."
                )
        record, _created = GoogleCredential.objects.get_or_create(account_email=account_email)
        record.set_credentials({
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": list(credentials.scopes) if credentials.scopes else scopes,
        })
        record.save()

        self.stdout.write(self.style.SUCCESS(
            f"Stored encrypted Google credentials for {account_email}."
        ))
