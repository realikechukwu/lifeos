"""Small presentation helpers for the family web interface: inline icons,
friendly due-date labels and assignee initials. Display only — no data
changes or business rules live here."""

from datetime import timedelta

from django import template
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from assistant.agent.formatting import to_web_html
from core.models import AssignedTo, Task

register = template.Library()

OPEN_STATUSES = (Task.Status.OPEN, Task.Status.IN_PROGRESS)


@register.simple_tag
def icon(name, size=20):
    """An inline SVG that references a symbol in web/_icons.html."""
    return format_html(
        '<svg class="icon" width="{0}" height="{0}" aria-hidden="true" focusable="false">'
        '<use href="#i-{1}"></use></svg>',
        size, name,
    )


@register.filter
def is_done(task):
    return task.status == Task.Status.COMPLETED


@register.filter
def is_open(task):
    return task.status in OPEN_STATUSES


@register.filter
def is_overdue(task):
    return bool(task.due_date) and task.status in OPEN_STATUSES and task.due_date < timezone.localdate()


@register.filter
def due_label(task):
    """"Today", "Tomorrow", "2 days overdue", "Fri", "Mon 14 Dec", plus the
    due time when there is one."""
    if not task.due_date:
        return "No date"
    today = timezone.localdate()
    delta = (task.due_date - today).days
    if delta < 0 and task.status in OPEN_STATUSES:
        label = "Yesterday" if delta == -1 else f"{-delta} days overdue"
    elif delta == 0:
        label = "Today"
    elif delta == 1:
        label = "Tomorrow"
    elif 1 < delta < 7:
        label = task.due_date.strftime("%A")
    elif task.due_date.year == today.year or task.due_date <= today + timedelta(days=180):
        label = task.due_date.strftime("%a %-d %b")
    else:
        label = task.due_date.strftime("%-d %b %Y")
    if task.due_time:
        label = f"{label} · {task.due_time.strftime('%H:%M')}"
    return label


@register.filter
def line_preview(text, length=160):
    """A one-paragraph preview of a note: list lines joined with dots, so a
    shopping list reads "Milk · Eggs · Bananas" instead of running together."""
    lines = [line.strip().lstrip("-•*").strip() for line in (text or "").splitlines()]
    joined = " · ".join(line for line in lines if line)
    return joined if len(joined) <= length else joined[: length - 1].rstrip() + "…"


@register.filter
def assignee_initial(value):
    """'I', 'W' or 'B' for a task's assignee value; empty when unassigned."""
    return {AssignedTo.IKE: "I", AssignedTo.WIFE: "W", AssignedTo.BOTH: "B"}.get(value, "")


@register.filter
def ask_text(value):
    """Render assistant plain text safely with **bold** and '• ' lists."""
    return mark_safe(to_web_html(value or ""))
