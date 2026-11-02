"""python manage.py homebase_check

Signs in to Homebase with the configured LifeOS account and counts the rows
the agent's Homebase tools can see. Run it before switching on
AGENT_HOMEBASE_ENABLED: an account that is not an approved owner signs in
fine but sees zero rows, because Homebase's row-level security hides them.

Prints counts only, never passwords, wifi details or policy numbers.
"""

from django.core.management.base import BaseCommand, CommandError

from assistant.services import homebase

TABLES = ["snags", "documents", "insurance_summary", "wifi_settings"]


class Command(BaseCommand):
    help = "Check the LifeOS Homebase sign-in and what it can read."

    def handle(self, *args, **options):
        if not homebase.configured():
            raise CommandError("Set HOMEBASE_SUPABASE_URL, HOMEBASE_SUPABASE_ANON_KEY, HOMEBASE_AGENT_EMAIL and HOMEBASE_AGENT_PASSWORD.")
        try:
            counts = {table: len(homebase.select(table, columns="updated_at", limit=1000)) for table in TABLES}
        except homebase.HomebaseError as exc:
            raise CommandError(str(exc)) from None
        for table, count in counts.items():
            self.stdout.write(f"{table}: {count}")
        if not any(counts.values()):
            raise CommandError("Signed in but every table is empty: check the account is an approved Homebase owner.")
        self.stdout.write(self.style.SUCCESS("Homebase sign-in works."))
