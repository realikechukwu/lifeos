"""Tools the agent can call, and their handlers.

Every handler takes the per-turn ``ToolContext`` plus the model's input and
returns a JSON string. Bad input raises ``ToolInputError``; the loop turns it
into an ``is_error`` tool result so the model can correct itself.

Definitions are built once, sorted by name, so the tools prefix of every
request stays byte-identical and cacheable.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Q

from core.models import AgentMemory, Note, Reminder, Task, TelegramChat, TelegramUser

from assistant.services.google_calendar import list_calendar_events
from assistant.services.telegram_inbox import calendar_items_between

logger = logging.getLogger(__name__)

MAX_CALENDAR_DAYS = 120
MAX_ACTIVE_MEMORIES = 100


class ToolInputError(ValueError):
    """The model sent unusable input; the message is shown back to it."""


@dataclass
class ToolContext:
    role: str
    speaker_name: str
    telegram_chat: TelegramChat | None = None
    telegram_user: TelegramUser | None = None
    telegram_update_id: int | None = None
    bot: object | None = None
    # Set once request_change has started a confirmation flow this turn.
    change_requested: bool = False
    events: list[str] = field(default_factory=list)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _parse_date(value, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ToolInputError(f"{name} must be a date in YYYY-MM-DD form.") from None


def get_calendar(ctx: ToolContext, args: dict) -> str:
    start = _parse_date(args.get("start_date"), "start_date")
    end = _parse_date(args.get("end_date"), "end_date")
    if end < start:
        raise ToolInputError("end_date must be on or after start_date.")
    if (end - start).days >= MAX_CALENDAR_DAYS:
        raise ToolInputError(f"Ask for at most {MAX_CALENDAR_DAYS} days at a time.")
    search = str(args.get("search") or "").strip()[:100]
    try:
        events = list_calendar_events(start, end + timedelta(days=1), query=search)
        return _dumps({"source": "google_calendar", "events": events})
    except Exception:  # noqa: BLE001 - degrade to LifeOS records rather than fail the answer
        logger.exception("Live calendar read failed; falling back to LifeOS records")
    items = calendar_items_between(start, end + timedelta(days=1))
    if search:
        items = [item for item in items if search.lower() in item.title.lower()]
    return _dumps({
        "source": "lifeos_records",
        "note": "The live Google Calendar could not be read, so this only lists events "
                "LifeOS created and imported work shifts. Say so if it matters.",
        "events": [
            {
                "title": item.title,
                "date": item.appointment_date,
                "start_time": item.start_time,
                "end_time": item.end_time,
                "all_day": item.all_day,
            }
            for item in items
        ],
    })


def list_tasks(ctx: ToolContext, args: dict) -> str:
    assigned_to = str(args.get("assigned_to") or "any")
    queryset = Task.objects.all()
    if not args.get("include_completed"):
        queryset = queryset.exclude(status__in=[Task.Status.COMPLETED, Task.Status.CANCELLED])
    if assigned_to != "any":
        queryset = queryset.filter(assigned_to=assigned_to)
    tasks = [
        {
            "id": task.id,
            "title": task.title,
            "description": task.description[:300],
            "assigned_to": task.assigned_to,
            "status": task.status,
            "priority": task.priority,
            "due_date": task.due_date,
            "due_time": task.due_time,
        }
        for task in queryset.order_by("-created_at")[:50]
    ]
    return _dumps({"tasks": tasks})


def list_reminders(ctx: ToolContext, args: dict) -> str:
    reminders = [
        {
            "id": reminder.id,
            "title": reminder.title,
            "message": reminder.message[:200],
            "recipient": reminder.recipient,
            "date": reminder.reminder_date,
            "time": reminder.reminder_time,
            "status": reminder.status,
        }
        for reminder in Reminder.objects.filter(
            status__in=[Reminder.Status.PENDING, Reminder.Status.FAILED]
        ).order_by("reminder_date", "reminder_time")[:50]
    ]
    return _dumps({"reminders": reminders})


def search_notes(ctx: ToolContext, args: dict) -> str:
    query = str(args.get("query") or "").strip()[:100]
    queryset = Note.objects.all()
    if query:
        queryset = queryset.filter(Q(title__icontains=query) | Q(body__icontains=query))
    notes = [
        {
            "id": note.id,
            "title": note.title,
            "category": note.category,
            "body": note.body[:2000],
            "updated_at": note.updated_at.date(),
        }
        for note in queryset.order_by("-updated_at")[:10]
    ]
    return _dumps({"notes": notes})


def remember(ctx: ToolContext, args: dict) -> str:
    text = " ".join(str(args.get("text") or "").split())
    if not text:
        raise ToolInputError("text must not be empty.")
    if len(text) > 500:
        raise ToolInputError("Keep a memory under 500 characters.")
    if AgentMemory.objects.filter(role=ctx.role, active=True).count() >= MAX_ACTIVE_MEMORIES:
        raise ToolInputError("The memory list is full; forget something out of date first.")
    memory = AgentMemory.objects.create(role=ctx.role, text=text)
    return _dumps({"saved": True, "memory_id": memory.id})


def forget(ctx: ToolContext, args: dict) -> str:
    try:
        memory_id = int(args.get("memory_id"))
    except (TypeError, ValueError):
        raise ToolInputError("memory_id must be a number from the saved memories list.") from None
    updated = AgentMemory.objects.filter(id=memory_id, role=ctx.role, active=True).update(active=False)
    if not updated:
        raise ToolInputError("No saved memory with that id for this person.")
    return _dumps({"forgotten": True})


def request_change(ctx: ToolContext, args: dict) -> str:
    instruction = str(args.get("instruction") or "").strip()
    if not instruction:
        raise ToolInputError("instruction must describe the change.")
    if ctx.change_requested:
        raise ToolInputError(
            "A change was already requested this turn. Everything must go in one instruction."
        )
    if ctx.telegram_chat is None or ctx.telegram_user is None or ctx.telegram_update_id is None:
        raise ToolInputError("Changes can only be requested from Telegram.")

    # Imported here: telegram_handlers imports the agent package for routing.
    from assistant.services.telegram_handlers import (
        has_pending_request,
        start_extraction_conversation,
    )

    if has_pending_request(ctx.telegram_chat, ctx.telegram_user):
        raise ToolInputError(
            "There is already a confirmation card waiting in this chat. Ask the speaker to "
            "confirm or cancel it first."
        )
    start_extraction_conversation(
        chat=ctx.telegram_chat,
        user=ctx.telegram_user,
        text=instruction[:4000],
        update_id=ctx.telegram_update_id,
        bot=ctx.bot,
    )
    ctx.change_requested = True
    ctx.events.append("change_requested")
    return _dumps({
        "status": "confirmation_card_shown",
        "detail": "The speaker now sees a card to confirm, edit or cancel. Nothing has changed yet.",
    })


_DATE = {"type": "string", "description": "A date in YYYY-MM-DD form."}

_DEFINITIONS = [
    {
        "name": "forget",
        "description": "Deactivate one of the speaker's saved memories by its id.",
        "input_schema": {
            "type": "object",
            "properties": {"memory_id": {"type": "integer"}},
            "required": ["memory_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_calendar",
        "description": (
            "Read the shared family Google Calendar live, including Ike's Patchwork work "
            "shifts, for an inclusive date range of up to 120 days. Recurring events are "
            "expanded. Optionally filter by text in the event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": _DATE,
                "end_date": {**_DATE, "description": "Last date to include, YYYY-MM-DD."},
                "search": {
                    "type": "string",
                    "description": "Text to match in event titles or details; empty for all events.",
                },
            },
            "required": ["start_date", "end_date", "search"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_reminders",
        "description": "List the pending LifeOS email reminders, soonest first.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_tasks",
        "description": "List LifeOS tasks, newest first, optionally for one person.",
        "input_schema": {
            "type": "object",
            "properties": {
                "assigned_to": {"type": "string", "enum": ["any", "ike", "wife", "both", "unassigned"]},
                "include_completed": {"type": "boolean"},
            },
            "required": ["assigned_to", "include_completed"],
            "additionalProperties": False,
        },
    },
    {
        "name": "remember",
        "description": (
            "Save a durable fact or preference about the speaker for future conversations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "One self-contained fact."}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_change",
        "description": (
            "Start LifeOS's confirmation flow for adding or changing household records: "
            "calendar events, tasks, notes, reminders, or completing a task. Call at most "
            "once per turn with every requested change in one complete instruction."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": (
                        "A complete, unambiguous instruction with absolute dates, 24-hour "
                        "times, who it is for and any reminder."
                    ),
                },
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_notes",
        "description": (
            "Search LifeOS notes by text in the title or body; an empty query returns the "
            "ten most recently updated notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]

TOOL_DEFINITIONS = sorted(_DEFINITIONS, key=lambda tool: tool["name"])

HANDLERS = {
    "forget": forget,
    "get_calendar": get_calendar,
    "list_reminders": list_reminders,
    "list_tasks": list_tasks,
    "remember": remember,
    "request_change": request_change,
    "search_notes": search_notes,
}
