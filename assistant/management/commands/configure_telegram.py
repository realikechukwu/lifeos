from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from assistant.services.telegram import TelegramBot


class Command(BaseCommand):
    help = "Configure the LifeOS Telegram webhook and command menu."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            default="",
            help="Public HTTPS webhook URL; defaults to APP_BASE_URL + /telegram/webhook/.",
        )

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN is not configured.")
        if not settings.TELEGRAM_WEBHOOK_SECRET:
            raise CommandError("TELEGRAM_WEBHOOK_SECRET is not configured.")
        url = options["url"].strip()
        if not url:
            if not settings.APP_BASE_URL:
                raise CommandError("Set APP_BASE_URL or pass --url.")
            url = settings.APP_BASE_URL.rstrip("/") + "/telegram/webhook/"
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise CommandError("Telegram webhook URL must be a public HTTPS URL.")

        bot = TelegramBot()
        identity = bot.call("getMe")
        bot.set_commands(
            [
                {"command": "start", "description": "Open the LifeOS menu"},
                {"command": "today", "description": "Show today's LifeOS view"},
                {"command": "week", "description": "Show the next 7 days"},
                {"command": "month", "description": "Show one month ahead"},
                {"command": "tasks", "description": "Show open tasks"},
                {"command": "calendar", "description": "Show upcoming calendar events"},
                {"command": "notes", "description": "Browse recent notes"},
                {"command": "reminders", "description": "Show pending reminders"},
                {"command": "search", "description": "Search open tasks and notes"},
                {"command": "settings", "description": "Manage daily briefing"},
                {"command": "upcoming", "description": "Show the next 14 days"},
                {"command": "cancel", "description": "Cancel your pending request"},
                {"command": "linkgroup", "description": "Link the family group (owner only)"},
                {"command": "help", "description": "Show help"},
            ]
        )
        bot.set_webhook(url, settings.TELEGRAM_WEBHOOK_SECRET)
        info = bot.get_webhook_info()
        username = identity.get("username", "configured bot")
        self.stdout.write(self.style.SUCCESS(f"Configured @{username} webhook: {info.get('url', url)}"))
