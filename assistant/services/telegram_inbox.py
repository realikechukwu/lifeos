"""Read and management helpers for the Telegram LifeOS inbox.

These helpers deliberately operate directly on the existing LifeOS models.
They never create ``IncomingEmail`` or ``ParsedAction`` records, keeping the
Telegram inbox separate from the email extraction/authorisation pipeline.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q
from django.utils import timezone

from core.models import AuditLog, CalendarEventRecord, Note, PatchworkShift, Reminder, Task


PAGE_SIZE = 6


@dataclass(frozen=True)
class TelegramCalendarItem:
    """A common read-only shape for LifeOS events and Patchwork shifts."""

    kind: str
    object_id: int
    title: str
    appointment_date: date
    start_time: time | None
    end_date: date | None
    end_time: time | None
    all_day: bool


def _aware(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time.min), timezone.get_current_timezone())


def _shift_zone(shift: PatchworkShift):
    try:
        return ZoneInfo(shift.timezone)
    except ZoneInfoNotFoundError:
        return timezone.get_current_timezone()


def calendar_item_for_shift(shift: PatchworkShift) -> TelegramCalendarItem:
    source_zone = _shift_zone(shift)
    local_start = timezone.localtime(shift.starts_at, source_zone)
    local_end = timezone.localtime(shift.ends_at, source_zone) if shift.ends_at else None
    return TelegramCalendarItem(
        kind="work_shift",
        object_id=shift.id,
        title=shift.display_title,
        appointment_date=local_start.date(),
        start_time=None if shift.all_day else local_start.timetz().replace(tzinfo=None),
        end_date=local_end.date() if local_end else None,
        end_time=(
            None
            if shift.all_day or local_end is None
            else local_end.timetz().replace(tzinfo=None)
        ),
        all_day=shift.all_day,
    )


def _calendar_item_for_event(event: CalendarEventRecord) -> TelegramCalendarItem:
    return TelegramCalendarItem(
        kind="calendar_event",
        object_id=event.id,
        title=event.title,
        appointment_date=event.appointment_date,
        start_time=event.start_time,
        end_date=event.appointment_date if event.end_time else None,
        end_time=event.end_time,
        all_day=event.all_day or event.start_time is None,
    )


def calendar_items(days: int = 30) -> list[TelegramCalendarItem]:
    """Merge ordinary events and active Patchwork shifts chronologically."""
    today = timezone.localdate()
    horizon = today + timedelta(days=days)
    items = [
        _calendar_item_for_event(event)
        for event in CalendarEventRecord.objects.filter(
            appointment_date__gte=today, appointment_date__lte=horizon
        )
    ]

    # Pad the UTC query by a day at each edge before applying each shift's
    # source timezone. This keeps boundary dates correct for non-UK feeds.
    shifts = PatchworkShift.objects.filter(
        active=True,
        suppressed=False,
        starts_at__gte=_aware(today - timedelta(days=1)),
        starts_at__lt=_aware(horizon + timedelta(days=2)),
    )
    for shift in shifts:
        item = calendar_item_for_shift(shift)
        if today <= item.appointment_date <= horizon:
            items.append(item)

    return sorted(
        items,
        key=lambda item: (
            item.appointment_date,
            item.start_time or time.min,
            item.kind,
            item.object_id,
        ),
    )


def _slice(queryset, page: int):
    page = max(page, 0)
    start = page * PAGE_SIZE
    items = list(queryset[start:start + PAGE_SIZE + 1])
    return items[:PAGE_SIZE], page, len(items) > PAGE_SIZE


def open_tasks(page: int = 0):
    today = timezone.localdate()
    queryset = Task.objects.exclude(status__in=[Task.Status.COMPLETED, Task.Status.CANCELLED]).order_by(
        "due_date", "due_time", "created_at"
    )
    # PostgreSQL sorts nulls last only when explicitly told to; split the
    # usual due-date ordering into a predictable cross-database list instead.
    tasks = list(queryset)
    tasks.sort(key=lambda task: (task.due_date is None, task.due_date or today, task.due_time or time.max))
    start = max(page, 0) * PAGE_SIZE
    items = tasks[start:start + PAGE_SIZE]
    return items, max(page, 0), len(tasks) > start + PAGE_SIZE


def upcoming_events(page: int = 0, days: int = 30):
    return _slice(calendar_items(days), page)


def recent_notes(page: int = 0, query: str = ""):
    queryset = Note.objects.all()
    query = query.strip()
    if query:
        queryset = queryset.filter(Q(title__icontains=query) | Q(body__icontains=query))
    return _slice(queryset.order_by("-updated_at"), page)


def pending_reminders(page: int = 0):
    return _slice(
        Reminder.objects.filter(status__in=[Reminder.Status.PENDING, Reminder.Status.FAILED]).order_by(
            "reminder_date", "reminder_time"
        ),
        page,
    )


def search_lifeos(query: str):
    """A compact, read-only cross-search used by the Telegram command."""
    query = query.strip()
    if not query:
        return [], []
    tasks = list(
        Task.objects.exclude(status__in=[Task.Status.COMPLETED, Task.Status.CANCELLED])
        .filter(Q(title__icontains=query) | Q(description__icontains=query))
        .order_by("due_date", "created_at")[:PAGE_SIZE]
    )
    notes = list(
        Note.objects.filter(Q(title__icontains=query) | Q(body__icontains=query))
        .order_by("-updated_at")[:PAGE_SIZE]
    )
    return tasks, notes


def today_items():
    """Return the exact sources shown in a private daily briefing."""
    today = timezone.localdate()
    events = calendar_items(days=0)
    tasks = list(
        Task.objects.exclude(status__in=[Task.Status.COMPLETED, Task.Status.CANCELLED]).filter(
            Q(due_date__lt=today) | Q(due_date=today)
        ).order_by("due_date", "due_time", "created_at")
    )
    reminders = list(
        Reminder.objects.filter(
            status__in=[Reminder.Status.PENDING, Reminder.Status.FAILED], reminder_date=today
        ).order_by("reminder_time")
    )
    return events, tasks, reminders


def update_note_body(note: Note, body: str) -> Note:
    body = body.strip()
    if not body:
        raise ValueError("A note cannot be empty.")
    note.body = body
    note.save(update_fields=["body", "updated_at"])
    AuditLog.objects.create(
        action="note_edited_via_telegram",
        object_type="Note",
        object_id=str(note.id),
        success=True,
        details={"title": note.title},
    )
    return note


def update_task(task: Task, *, title: str, due_date=None, assigned_to: str = "") -> Task:
    title = title.strip()
    if not title:
        raise ValueError("A task needs a title.")
    if assigned_to and assigned_to not in {choice for choice, _ in Task._meta.get_field("assigned_to").choices}:
        raise ValueError("Choose ike, wife, both, or unassigned for the assignee.")
    task.title = title
    task.due_date = due_date
    task.due_time = None
    if assigned_to:
        task.assigned_to = assigned_to
    task.save(update_fields=["title", "due_date", "due_time", "assigned_to", "updated_at"])
    AuditLog.objects.create(
        action="task_edited_via_telegram",
        object_type="Task",
        object_id=str(task.id),
        success=True,
        details={"title": task.title, "due_date": str(task.due_date or ""), "assigned_to": task.assigned_to},
    )
    return task


def snooze_reminder(reminder: Reminder, days: int = 1) -> Reminder:
    if reminder.status not in [Reminder.Status.PENDING, Reminder.Status.FAILED]:
        raise ValueError("Only unsent reminders can be snoozed.")
    reminder.reminder_date = max(reminder.reminder_date, timezone.localdate()) + timedelta(days=days)
    reminder.status = Reminder.Status.PENDING
    reminder.last_error = ""
    reminder.save(update_fields=["reminder_date", "status", "last_error", "updated_at"])
    AuditLog.objects.create(
        source_email=reminder.source_email,
        action="reminder_snoozed_via_telegram",
        object_type="Reminder",
        object_id=str(reminder.id),
        success=True,
        details={"reminder_date": str(reminder.reminder_date)},
    )
    return reminder


def cancel_reminder(reminder: Reminder) -> Reminder:
    if reminder.status not in [Reminder.Status.PENDING, Reminder.Status.FAILED]:
        raise ValueError("Only unsent reminders can be cancelled.")
    reminder.status = Reminder.Status.CANCELLED
    reminder.save(update_fields=["status", "updated_at"])
    AuditLog.objects.create(
        source_email=reminder.source_email,
        action="reminder_cancelled_via_telegram",
        object_type="Reminder",
        object_id=str(reminder.id),
        success=True,
        details={"title": reminder.title},
    )
    return reminder
