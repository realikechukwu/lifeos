"""Read-only aggregation for the family web dashboard (Phase 3).

Deliberately contains no email-parsing / creation / approval business
logic — that all still lives in `assistant/services/*`. This module only
reads and re-labels data that Phase 1/2 already produced
(CalendarEventRecord, Task, Reminder, ParsedAction) for display.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from assistant.services.google_calendar import build_google_calendar_event_url
from core.models import CalendarEventRecord, ParsedAction, PatchworkShift, Reminder, Task

UPCOMING_WINDOW_DAYS = 30


def _aware(d: date | None, t: time | None = None) -> datetime | None:
    if d is None:
        return None
    naive = datetime.combine(d, t or time.min)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _far_future() -> datetime:
    return timezone.now() + timedelta(days=3650)


def _format_when(d: date | None, t: time | None, all_day: bool) -> str:
    if d is None:
        return ""
    label = d.strftime("%a %d %b %Y")
    if all_day:
        return f"{label} (all day)"
    if t:
        return f"{label} {t.strftime('%H:%M')}"
    return label


def _shift_local_datetime(shift: PatchworkShift) -> datetime:
    """Render stored UTC shift times in their source timezone."""
    try:
        source_zone = ZoneInfo(shift.timezone)
    except ZoneInfoNotFoundError:
        source_zone = timezone.get_current_timezone()
    return timezone.localtime(shift.starts_at, source_zone)


@dataclass
class FeedItem:
    kind: str            # calendar_event | work_shift | task_due | task_overdue | reminder | review
    kind_label: str
    title: str
    when: datetime | None
    when_label: str
    assignee: str
    status: str
    location: str
    detail_url: str | None
    is_overdue: bool = False
    is_attention: bool = False
    display_when: str = ""
    details_when: str = ""
    display_date: date | None = None
    extra_links: list = field(default_factory=list)  # [(label, url), ...]


@dataclass
class FeedSection:
    date: date
    label: str
    items: list[FeedItem]
    is_today: bool = False


def _relative_when(d: date | None, t: time | None, *, today: date, all_day: bool = False) -> str:
    if d is None:
        return ""
    day_delta = (d - today).days
    if day_delta < 0:
        day_label = f"{abs(day_delta)} day{'s' if day_delta != -1 else ''} overdue"
    elif day_delta == 0:
        day_label = "Today"
    elif day_delta == 1:
        day_label = "Tomorrow"
    else:
        day_label = f"In {day_delta} days"
    if all_day:
        return f"{day_label} · All day"
    if t:
        return f"{day_label} · {t.strftime('%H:%M')}"
    return day_label


def build_upcoming_sections(items: list[FeedItem], *, today: date | None = None) -> list[FeedSection]:
    """Group non-attention feed items into dated agenda sections.

    Today is always returned so the landing page retains a stable point of
    orientation, even on a quiet day. Undated items belong in the attention
    collection and are deliberately omitted here.
    """
    today = today or timezone.localdate()
    by_date: dict[date, list[FeedItem]] = {today: []}
    for item in items:
        if item.is_attention or item.when is None:
            continue
        item_date = item.display_date or timezone.localtime(item.when).date()
        by_date.setdefault(item_date, []).append(item)

    sections = []
    for section_date in sorted(by_date):
        if section_date == today:
            label = "Today"
        elif section_date == today + timedelta(days=1):
            label = "Tomorrow"
        else:
            label = section_date.strftime("%A %-d %B")
        sections.append(FeedSection(
            date=section_date,
            label=label,
            items=by_date[section_date],
            is_today=section_date == today,
        ))
    return sections


def build_upcoming_feed(user, *, window_days: int = UPCOMING_WINDOW_DAYS) -> list[FeedItem]:
    """Chronological feed combining Google Calendar events (next
    `window_days`), open tasks due in that window, overdue open tasks,
    pending reminders, and pending-review actions. Overdue tasks are sorted
    to the top; everything else is chronological."""
    today = timezone.localdate()
    horizon = today + timedelta(days=window_days)
    items: list[FeedItem] = []

    events = CalendarEventRecord.objects.filter(
        appointment_date__gte=today, appointment_date__lte=horizon
    )
    for event in events:
        gcal_url = build_google_calendar_event_url(event.google_event_id, event.calendar_id)
        title = event.title
        if event.recurrence_description:
            title = f"{title} — {event.recurrence_description}"
        details_when = _format_when(event.appointment_date, event.start_time, event.all_day)
        if not event.all_day and event.end_time:
            details_when += f" – {event.end_time.strftime('%H:%M')}"
            if event.start_time and event.end_time < event.start_time:
                details_when += " (next day)"
        items.append(FeedItem(
            kind="calendar_event",
            kind_label="Calendar event",
            title=title,
            when=_aware(event.appointment_date, event.start_time),
            when_label=_format_when(event.appointment_date, event.start_time, event.all_day),
            details_when=details_when,
            assignee="",
            status="Scheduled",
            display_when=_relative_when(
                event.appointment_date, event.start_time, today=today, all_day=event.all_day
            ),
            location=event.location,
            detail_url=None,
            extra_links=[("Open in Google Calendar", gcal_url)] if gcal_url else [],
        ))

    work_shifts = PatchworkShift.objects.filter(active=True)
    start_of_window = _aware(today)
    end_of_window = _aware(horizon + timedelta(days=1))
    work_shifts = work_shifts.filter(starts_at__gte=start_of_window, starts_at__lt=end_of_window)
    for shift in work_shifts:
        local_start = _shift_local_datetime(shift) if shift.all_day else timezone.localtime(shift.starts_at)
        details_when = _format_when(local_start.date(), local_start.time(), shift.all_day)
        if shift.ends_at:
            local_end = timezone.localtime(shift.ends_at, local_start.tzinfo)
            if shift.all_day:
                last_day = (local_end - timedelta(microseconds=1)).date()
                if last_day > local_start.date():
                    details_when = f"{local_start.strftime('%a %d %b')} – {last_day.strftime('%a %d %b %Y')} (all day)"
            else:
                details_when += " – " + (local_end.strftime('%a %d %b %H:%M') if local_end.date() != local_start.date() else local_end.strftime('%H:%M'))
        gcal_url = build_google_calendar_event_url(shift.google_event_id, shift.calendar_id)
        items.append(FeedItem(
            kind="work_shift",
            kind_label="Ike work shift",
            title=shift.display_title,
            when=shift.starts_at,
            display_date=local_start.date(),
            details_when=details_when,
            when_label=_format_when(
                local_start.date(), local_start.timetz().replace(tzinfo=None), shift.all_day
            ),
            assignee="Ike",
            status="Scheduled",
            display_when=_relative_when(
                local_start.date(), local_start.timetz().replace(tzinfo=None),
                today=today, all_day=shift.all_day,
            ),
            location="",
            detail_url=None,
            extra_links=[("Open in Google Calendar", gcal_url)] if gcal_url else [],
        ))

    open_tasks = Task.objects.filter(
        status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS], due_date__isnull=False
    )
    for task in open_tasks:
        overdue = task.due_date < today
        if not overdue and task.due_date > horizon:
            continue
        items.append(FeedItem(
            kind="task_overdue" if overdue else "task_due",
            kind_label="Overdue task" if overdue else "Task due",
            title=task.title,
            when=_aware(task.due_date, task.due_time),
            when_label=_format_when(task.due_date, task.due_time, False),
            assignee=task.get_assigned_to_display(),
            status=task.get_status_display(),
            location="",
            detail_url=reverse("web:task_list") + f"?highlight={task.id}",
            is_overdue=overdue,
            is_attention=overdue,
            display_when=_relative_when(task.due_date, task.due_time, today=today),
        ))

    for reminder in Reminder.objects.filter(
        status=Reminder.Status.PENDING, reminder_date__lte=horizon
    ):
        overdue = _aware(reminder.reminder_date, reminder.reminder_time) < timezone.now()
        display_when = _relative_when(reminder.reminder_date, reminder.reminder_time, today=today)
        if overdue and reminder.reminder_date == today:
            display_when = f"Due earlier today · {reminder.reminder_time.strftime('%H:%M')}"
        items.append(FeedItem(
            kind="reminder",
            kind_label="Reminder",
            title=reminder.title,
            when=_aware(reminder.reminder_date, reminder.reminder_time),
            when_label=_format_when(reminder.reminder_date, reminder.reminder_time, False),
            assignee=reminder.get_recipient_display(),
            status="Overdue" if overdue else reminder.get_status_display(),
            location="",
            detail_url=None,
            is_overdue=overdue,
            is_attention=overdue,
            display_when=display_when,
        ))

    review_actions = ParsedAction.objects.filter(status=ParsedAction.Status.PENDING_REVIEW).select_related(
        "incoming_email"
    )
    for action in review_actions:
        received = action.incoming_email.received_at if action.incoming_email else None
        detail_url = reverse("web:review_detail", args=[action.id]) if user.is_staff else reverse("web:review_list")
        items.append(FeedItem(
            kind="review",
            kind_label="Needs review",
            title=action.title or action.get_action_type_display(),
            when=received,
            when_label=f"Received {received.strftime('%a %d %b %Y %H:%M')}" if received else "",
            assignee="",
            status="Pending review",
            location="",
            detail_url=detail_url,
            is_attention=True,
            display_when=(
                f"Received today · {timezone.localtime(received).strftime('%H:%M')}"
                if received and timezone.localtime(received).date() == today
                else f"Received {timezone.localtime(received).strftime('%-d %b')}"
                if received else "Awaiting review"
            ),
        ))

    far_future = _far_future()

    def sort_key(item: FeedItem):
        return (0 if item.is_overdue else 1, item.when or far_future)

    items.sort(key=sort_key)
    return items


def parse_date_param(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _combine_iso(d: date, t: time | None) -> str:
    if t is None:
        return d.isoformat()
    return f"{d.isoformat()}T{t.strftime('%H:%M:%S')}"


def build_calendar_feed(start: date | None, end: date | None) -> list[dict]:
    """FullCalendar-compatible event list, combining Google Calendar events,
    task due dates, and pending/sent reminder dates. `end` is treated as
    exclusive, matching FullCalendar's own range convention."""
    events: list[dict] = []

    calendar_qs = CalendarEventRecord.objects.all()
    if start:
        calendar_qs = calendar_qs.filter(
            Q(appointment_date__gte=start) |
            Q(appointment_date=start - timedelta(days=1), all_day=False,
              end_time__lt=F("start_time"), end_time__gt=time.min)
        )
    if end:
        calendar_qs = calendar_qs.filter(appointment_date__lt=end)
    for event in calendar_qs:
        if event.all_day:
            fc_start, fc_end, all_day = event.appointment_date.isoformat(), None, True
        else:
            all_day = False
            fc_start = _combine_iso(event.appointment_date, event.start_time)
            fc_end = _combine_iso(event.appointment_date, event.end_time) if event.end_time else None
            if event.start_time and event.end_time and event.end_time < event.start_time:
                fc_end = _combine_iso(event.appointment_date + timedelta(days=1), event.end_time)
        title = event.title
        if event.recurrence_description:
            title = f"{title} — {event.recurrence_description}"
        events.append({
            "id": f"calendar-{event.id}",
            "title": title,
            "start": fc_start,
            "end": fc_end,
            "allDay": all_day,
            "color": "#2563eb",
            "extendedProps": {
                "type": "Calendar event",
                "category": "event",
                "status": "Scheduled",
                "location": event.location,
                "assignee": "",
                "googleUrl": build_google_calendar_event_url(event.google_event_id, event.calendar_id) or "",
                "recurring": bool(event.recurrence_rule),
                "recurrenceDescription": event.recurrence_description or "",
            },
        })

    work_shift_qs = PatchworkShift.objects.filter(active=True)
    if start:
        # Include shifts already in progress at the start of the visible range.
        work_shift_qs = work_shift_qs.filter(
            Q(starts_at__gte=_aware(start)) | Q(ends_at__gt=_aware(start))
        )
    if end:
        work_shift_qs = work_shift_qs.filter(starts_at__lt=_aware(end))
    for shift in work_shift_qs:
        local_start = _shift_local_datetime(shift) if shift.all_day else timezone.localtime(shift.starts_at)
        local_end = timezone.localtime(shift.ends_at, local_start.tzinfo) if shift.ends_at else None
        if shift.all_day:
            fc_start = local_start.date().isoformat()
            fc_end = local_end.date().isoformat() if local_end else None
        else:
            fc_start = local_start.isoformat()
            fc_end = local_end.isoformat() if local_end else None
        events.append({
            "id": f"patchwork-{shift.id}",
            "title": shift.display_title,
            "start": fc_start,
            "end": fc_end,
            "allDay": shift.all_day,
            "color": "#7c3aed",
            "extendedProps": {
                "type": "Ike work shift",
                "category": "work",
                "status": "Scheduled",
                "location": "",
                "assignee": "Ike",
                "googleUrl": build_google_calendar_event_url(shift.google_event_id, shift.calendar_id) or "",
                "readOnly": True,
            },
        })

    task_qs = Task.objects.exclude(status=Task.Status.CANCELLED).filter(due_date__isnull=False)
    if start:
        task_qs = task_qs.filter(due_date__gte=start)
    if end:
        task_qs = task_qs.filter(due_date__lt=end)
    today = timezone.localdate()
    for task in task_qs:
        overdue = task.due_date < today and task.status in (Task.Status.OPEN, Task.Status.IN_PROGRESS)
        events.append({
            "id": f"task-{task.id}",
            "title": f"Task due: {task.title}",
            "start": _combine_iso(task.due_date, task.due_time),
            "allDay": task.due_time is None,
            "color": "#dc2626" if overdue else "#f59e0b",
            "textColor": "#ffffff" if overdue else "#29200c",
            "extendedProps": {
                "type": "Task due date",
                "category": "task",
                "isOverdue": overdue,
                "detailUrl": reverse("web:task_list") + f"?highlight={task.id}",
                "status": task.get_status_display(),
                "location": "",
                "assignee": task.get_assigned_to_display(),
                "googleUrl": "",
            },
        })

    reminder_qs = Reminder.objects.filter(status__in=[Reminder.Status.PENDING, Reminder.Status.SENT])
    if start:
        reminder_qs = reminder_qs.filter(reminder_date__gte=start)
    if end:
        reminder_qs = reminder_qs.filter(reminder_date__lt=end)
    for reminder in reminder_qs:
        events.append({
            "id": f"reminder-{reminder.id}",
            "title": f"Reminder: {reminder.title}",
            "start": _combine_iso(reminder.reminder_date, reminder.reminder_time),
            "allDay": False,
            "color": "#16a34a",
            "textColor": "#08220f",
            "extendedProps": {
                "type": "Reminder",
                "category": "reminder",
                "status": reminder.get_status_display(),
                "location": "",
                "assignee": reminder.get_recipient_display(),
                "googleUrl": "",
            },
        })

    return events
