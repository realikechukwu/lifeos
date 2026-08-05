"""python manage.py google_auth

Runs the local browser-based OAuth consent flow for the assistant Gmail /
Calendar account and stores the resulting credentials, Fernet-encrypted, in
GoogleCredential. Intended to be run on a machine with a browser (your local
computer) — not on the Oracle server."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from google_auth_oauthlib.flow import InstalledAppFlow

from core.models import GoogleCredential

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
]


class Command(BaseCommand):
    help = "Authorise the assistant Google account via a local browser OAuth flow."

    def handle(self, *args, **options):
        if settings.GOOGLE_CLIENT_SECRETS_FILE:
            flow = InstalledAppFlow.from_client_secrets_file(
                settings.GOOGLE_CLIENT_SECRETS_FILE, scopes=SCOPES
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
            flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
        else:
            raise CommandError(
                "Set GOOGLE_CLIENT_SECRETS_FILE, or both GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET, in your environment before running google_auth."
            )

        self.stdout.write("Opening your browser to sign in to Google...")
        credentials = flow.run_local_server(port=0)

        account_email = settings.GOOGLE_ASSISTANT_EMAIL
        record, _created = GoogleCredential.objects.get_or_create(account_email=account_email)
        record.set_credentials({
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": list(credentials.scopes) if credentials.scopes else SCOPES,
        })
        record.save()

        self.stdout.write(self.style.SUCCESS(
            f"Stored encrypted Google credentials for {account_email}."
        ))
