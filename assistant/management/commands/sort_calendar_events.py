"""Sort the events already on the main calendar into the category calendars.

A one-off for switching to colour-coded calendars. Without --apply it is a
trial run: it prints where every event would go and changes nothing. With
--apply it moves each event with Google's move call, so the event keeps its
ID, LifeOS keeps track of it and no copies are left behind.

Events that are not Patchwork shifts and had no category saved when LifeOS
added them are sorted by the OpenAI model LifeOS already uses for extraction
(titles and locations only). Pass --no-ai to use the title keyword rules
instead; anything they cannot place stays personal.
"""

from collections import Counter
from datetime import date, datetime, time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from googleapiclient.discovery import build

from assistant.services.calendar_categories import (
    CATEGORIES, FAMILY, HEALTH, PERSONAL, WORK, calendar_id_for, classify_with_ai, guess_category,
    separate_calendars_configured,
)
from assistant.services.gmail import get_google_credentials
from core.models import AuditLog, CalendarEventRecord, ParsedAction, PatchworkShift

GOOGLE_API_RETRIES = 5
ORDER = (WORK, FAMILY, HEALTH, PERSONAL)
MOVABLE_EVENT_TYPES = {"", "default"}


def list_source_events(service, calendar_id: str, since: date | None) -> list[dict]:
    """Every live event on the calendar, recurring series as one item each."""
    params = {"calendarId": calendar_id, "maxResults": 2500, "showDeleted": False}
    if since:
        zone = timezone.get_current_timezone()
        params["timeMin"] = timezone.make_aware(datetime.combine(since, time.min), zone).isoformat()
    events, page_token = [], None
    while True:
        response = service.events().list(**params, pageToken=page_token).execute(num_retries=GOOGLE_API_RETRIES)
        events += [item for item in response.get("items", []) if item.get("id") and item.get("status") != "cancelled"]
        page_token = response.get("nextPageToken")
        if not page_token:
            return events


def _start_label(item: dict) -> str:
    start = item.get("start", {})
    return (start.get("date") or start.get("dateTime") or "")[:10] or "????-??-??"


def plan_sort(service, source: str, *, since=None, use_ai=True, overrides=None) -> tuple[list[dict], list[dict]]:
    """Decide a category for each event. Returns (plan, skipped)."""
    overrides = overrides or {}
    shift_ids = set(PatchworkShift.objects.exclude(google_event_id="").values_list("google_event_id", flat=True))
    saved = {
        event_id: category
        for event_id, category in CalendarEventRecord.objects.filter(calendar_id=source)
        .exclude(parsed_action__calendar_category="")
        .values_list("google_event_id", "parsed_action__calendar_category")
    }
    plan, skipped, unplaced = [], [], []
    for item in list_source_events(service, source, since):
        row = {
            "id": item["id"],
            "title": item.get("summary") or "(no title)",
            "location": item.get("location") or "",
            "date": _start_label(item),
            "repeats": bool(item.get("recurrence")),
        }
        if item.get("recurringEventId"):
            continue  # A changed occurrence; it moves with its series.
        if item.get("eventType", "") not in MOVABLE_EVENT_TYPES:
            skipped.append({**row, "why": f"Google does not allow moving {item['eventType']} events"})
            continue
        if item.get("organizer", {}).get("self") is False:
            skipped.append({**row, "why": "an invitation from someone else; only its organiser can move it"})
            continue
        if row["id"] in overrides:
            row.update(category=overrides[row["id"]], reason="you")
        elif row["id"] in shift_ids:
            row.update(category=WORK, reason="shift")
        elif row["id"] in saved:
            row.update(category=saved[row["id"]], reason="saved")
        else:
            unplaced.append(row)
        plan.append(row)

    # The AI reads the whole title in context, so it goes first; the keyword
    # rules only fill in when it is switched off or cannot be reached.
    categories = None
    if unplaced and use_ai:
        try:
            categories = classify_with_ai([{"title": r["title"], "location": r["location"]} for r in unplaced])
        except Exception:  # noqa: BLE001 - a trial run should still show the rest of the plan
            categories = None
    for position, row in enumerate(unplaced):
        if categories is not None:
            row.update(category=categories[position], reason="ai")
        elif guess := guess_category(row["title"], row["location"]):
            row.update(category=guess, reason="keywords")
        else:
            row.update(category=PERSONAL, reason="default")
    plan.sort(key=lambda r: (ORDER.index(r["category"]), r["date"], r["title"].lower()))
    return plan, skipped


def apply_moves(service, source: str, plan: list[dict]) -> tuple[Counter, list[dict]]:
    moved, failed = Counter(), []
    for row in plan:
        destination = calendar_id_for(row["category"])
        if destination == source:
            continue
        try:
            service.events().move(
                calendarId=source, eventId=row["id"], destination=destination
            ).execute(num_retries=GOOGLE_API_RETRIES)
        except Exception as exc:  # noqa: BLE001 - report each failure and carry on with the rest
            failed.append({**row, "why": str(exc)[:300]})
            continue
        CalendarEventRecord.objects.filter(google_event_id=row["id"], calendar_id=source).update(
            calendar_id=destination
        )
        ParsedAction.objects.filter(
            calendar_events__google_event_id=row["id"], calendar_category=""
        ).update(calendar_category=row["category"])
        PatchworkShift.objects.filter(google_event_id=row["id"], calendar_id=source).update(
            calendar_id=destination
        )
        moved[row["category"]] += 1
    return moved, failed


class Command(BaseCommand):
    help = (
        "Sort events on the main calendar into the work, family and health calendars. "
        "A trial run unless --apply is given."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Move the events. Without this nothing changes.")
        parser.add_argument(
            "--no-ai", action="store_true",
            help="Use only the keyword rules; anything they cannot place stays personal.",
        )
        parser.add_argument("--since", help="Only events from this date on (YYYY-MM-DD). Default: every event.")
        parser.add_argument(
            "--set", action="append", default=[], metavar="EVENT_ID=CATEGORY",
            help="Put one event in a category of your choosing. Repeat as needed.",
        )

    def handle(self, *args, **options):
        if not separate_calendars_configured():
            raise CommandError(
                "No category calendars are set. Add GOOGLE_CALENDAR_ID_WORK, _FAMILY and/or _HEALTH first."
            )
        source = settings.GOOGLE_CALENDAR_ID
        since = None
        if options["since"]:
            try:
                since = date.fromisoformat(options["since"])
            except ValueError:
                raise CommandError("--since must be a date in YYYY-MM-DD form.") from None
        overrides = {}
        for value in options["set"]:
            event_id, _, category = value.partition("=")
            if not event_id or category not in CATEGORIES:
                raise CommandError(f"--set {value!r}: use EVENT_ID=work|family|health|personal.")
            overrides[event_id] = category

        service = build("calendar", "v3", credentials=get_google_credentials(), cache_discovery=False)
        plan, skipped = plan_sort(service, source, since=since, use_ai=not options["no_ai"], overrides=overrides)
        self._print_plan(plan, skipped)

        if not options["apply"]:
            self.stdout.write("\nTrial run: nothing was moved. Re-run with --apply to move these events.")
            return

        moved, failed = apply_moves(service, source, plan)
        AuditLog.objects.create(
            action="sort_calendar_events",
            object_type="CalendarEventRecord",
            success=not failed,
            details={"moved": dict(moved), "failed": len(failed), "skipped": len(skipped)},
        )
        self.stdout.write(self.style.SUCCESS(
            "\nMoved " + ", ".join(f"{moved[c]} to {c}" for c in (WORK, FAMILY, HEALTH)) + "."
        ))
        for row in failed:
            self.stdout.write(self.style.WARNING(f"  Not moved: {row['date']}  {row['title']}  ({row['why']})"))

    def _print_plan(self, plan, skipped):
        counts = Counter(row["category"] for row in plan)
        for category in ORDER:
            rows = [row for row in plan if row["category"] == category]
            if not rows:
                continue
            destination = calendar_id_for(category)
            where = "stays on the main calendar" if destination == settings.GOOGLE_CALENDAR_ID else "moves"
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{category.upper()} ({len(rows)}, {where})"))
            for row in rows:
                repeats = " (repeats)" if row["repeats"] else ""
                self.stdout.write(f"  {row['date']}  {row['title']}{repeats}  [{row['reason']}]  id={row['id']}")
        if skipped:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\nLEFT WHERE THEY ARE ({len(skipped)})"))
            for row in skipped:
                self.stdout.write(f"  {row['date']}  {row['title']}  ({row['why']})")
        self.stdout.write(
            "\nTotal: " + ", ".join(f"{counts[c]} {c}" for c in ORDER)
            + ". Reasons: shift = Patchwork shift, saved = chosen when it was added, ai = sorted by the AI, "
            "keywords = title rules (AI off or unavailable), default = nothing matched, you = --set."
        )
