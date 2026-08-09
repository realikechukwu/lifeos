"""Synchronise the private Patchwork work-shift calendar into LifeOS."""

from django.core.management.base import BaseCommand, CommandError

from assistant.services.patchwork_calendar import sync_patchwork_calendar
from core.models import AuditLog


class Command(BaseCommand):
    help = "Sync Patchwork shifts into the shared Google Calendar and LifeOS."

    def handle(self, *args, **options):
        try:
            result = sync_patchwork_calendar()
        except Exception as exc:  # noqa: BLE001 - command must fail visibly for systemd retries/alerts
            AuditLog.objects.create(
                action="sync_patchwork_calendar",
                object_type="PatchworkShift",
                success=False,
                error_message=str(exc)[:2000],
            )
            raise CommandError("Patchwork calendar sync failed; see the audit log for details.") from exc

        AuditLog.objects.create(
            action="sync_patchwork_calendar",
            object_type="PatchworkShift",
            success=True,
            details={
                "created": result.created,
                "updated": result.updated,
                "deactivated": result.deactivated,
                "unchanged": result.unchanged,
            },
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Patchwork sync: {result.created} created, {result.updated} updated, "
                f"{result.deactivated} deactivated, {result.unchanged} unchanged."
            )
        )
