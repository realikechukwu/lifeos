"""Telegram webhook orchestration and conversational interaction design."""

import html
import logging
import re
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import (
    CalendarEventRecord,
    HouseholdMember,
    IncomingEmail,
    Note,
    PatchworkShift,
    ParsedAction,
    Reminder,
    Task,
    TelegramInboxAction,
    TelegramChat,
    TelegramConversation,
    TelegramPreference,
    TelegramUpdate,
    TelegramUser,
)

from .router import admin_approve_and_execute, build_parsed_action_from_extraction
from .tasks import complete_task
from .telegram import TelegramAPIError, TelegramBot, TelegramFileTooLargeError
from .telegram_extractor import extract_telegram_action
from .telegram_transcription import TelegramTranscriptionError, transcribe_telegram_voice
from .telegram_inbox import (
    PAGE_SIZE,
    calendar_item_for_shift,
    calendar_items,
    cancel_reminder,
    one_month_after,
    open_tasks,
    pending_reminders,
    planning_items,
    recent_notes,
    search_lifeos,
    snooze_reminder,
    today_items,
    upcoming_events,
    update_note_body,
    update_task,
)
from .google_calendar import cancel_calendar_event, reschedule_calendar_event

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (
    TelegramConversation.Status.AWAITING_CLARIFICATION,
    TelegramConversation.Status.AWAITING_CONFIRMATION,
)

# Voice notes are deliberately short and bounded before any transcription work.
MAX_VOICE_DURATION_SECONDS = 120
MAX_VOICE_DOWNLOAD_BYTES = 5 * 1024 * 1024
# A crashed worker can be retried, while concurrent deliveries cannot double-process.
UPDATE_PROCESSING_LEASE = timedelta(minutes=10)


class TelegramVoiceProcessingError(RuntimeError):
    """A redacted transient failure after voice transcription."""


def _button(text: str, callback_data: str) -> dict:
    return {"text": text, "callback_data": callback_data}


def _keyboard(*rows: list[dict]) -> dict:
    return {"inline_keyboard": list(rows)}


def main_menu() -> dict:
    return _keyboard(
        # The top rows are for viewing/managing existing LifeOS items.
        [_button("☀️ Today", "tg:today:show"), _button("📋 Tasks", "tg:inbox:tasks:0")],
        [_button("📆 Week", "tg:plan:weekly"), _button("🗓 Month", "tg:plan:monthly")],
        [_button("🗓 Calendar", "tg:inbox:calendar:0"), _button("📚 Notes", "tg:inbox:notes:0")],
        [_button("🔔 Reminders", "tg:inbox:reminders:0"), _button("⚙️ Briefing", "tg:settings:show")],
        # Every creation action starts with the same visual cue.
        [_button("➕ Task", "tg:new:task"), _button("➕ Event", "tg:new:event")],
        [_button("➕ Note", "tg:new:note"), _button("➕ Reminder", "tg:new:reminder")],
    )


MAIN_MENU_PROMPT = (
    "What would you like to do?\n\n"
    "<b>View & manage</b> uses the top three rows.\n"
    "<b>➕ Add something new</b> uses the bottom two rows."
)

HOME_CALLBACK = "tg:nav:home"


def _navigation_rows(back_callback: str = HOME_CALLBACK, *, back_label: str = "⬅️ Back") -> list[list[dict]]:
    """Consistent navigation footer for every screen below the main menu."""
    return [[
        _button(back_label, back_callback),
        _button("🏠 Home", HOME_CALLBACK),
    ]]


def _navigation_keyboard(back_callback: str = HOME_CALLBACK, *, back_label: str = "⬅️ Back") -> dict:
    return _keyboard(*_navigation_rows(back_callback, back_label=back_label))


def _origin_callback(origin: str) -> str:
    return "tg:nav:today" if origin == "today" else HOME_CALLBACK


def _configured_role(user_id: int) -> str | None:
    if user_id and user_id == settings.TELEGRAM_IKE_USER_ID:
        return HouseholdMember.Role.IKE
    if user_id and user_id == settings.TELEGRAM_WIFE_USER_ID:
        return HouseholdMember.Role.WIFE
    return None


def _upsert_user(data: dict) -> TelegramUser | None:
    user_id = data.get("id")
    role = _configured_role(user_id)
    if role is None or data.get("is_bot"):
        return None
    display_name = " ".join(
        part for part in [data.get("first_name", ""), data.get("last_name", "")] if part
    ).strip()
    user, _ = TelegramUser.objects.update_or_create(
        user_id=user_id,
        defaults={
            "role": role,
            "username": str(data.get("username", ""))[:64],
            "display_name": display_name[:255],
            "authorised": True,
        },
    )
    return user


def _upsert_chat(data: dict, user: TelegramUser) -> TelegramChat:
    chat_id = data["id"]
    chat_type = data.get("type", "private")
    configured_groups = set(settings.TELEGRAM_ALLOWED_GROUP_CHAT_IDS)
    authorised = chat_type == TelegramChat.ChatType.PRIVATE and chat_id == user.user_id
    if chat_type in (TelegramChat.ChatType.GROUP, TelegramChat.ChatType.SUPERGROUP):
        authorised = chat_id in configured_groups
    existing = TelegramChat.objects.filter(chat_id=chat_id).first()
    if existing and existing.authorised:
        authorised = True
    chat, _ = TelegramChat.objects.update_or_create(
        chat_id=chat_id,
        defaults={
            "chat_type": chat_type,
            "title": str(data.get("title", ""))[:255],
            "authorised": authorised,
        },
    )
    return chat


def _authorised_email_for_role(role: str) -> str:
    configured = (
        settings.AUTHORISED_EMAIL_IKE
        if role == HouseholdMember.Role.IKE
        else settings.AUTHORISED_EMAIL_WIFE
    )
    if configured:
        return configured
    member = HouseholdMember.objects.filter(role=role, authorised=True, active=True).first()
    return member.email if member else f"telegram-{role}@lifeos.invalid"


def _new_conversation(
    chat: TelegramChat, user: TelegramUser, text: str, update_id: int
) -> TelegramConversation:
    incoming = IncomingEmail.objects.create(
        gmail_message_id=f"telegram:{update_id}",
        source=IncomingEmail.Source.TELEGRAM,
        outer_sender=_authorised_email_for_role(user.role),
        recipients="LifeOS Telegram bot",
        subject=f"Telegram request from {user.display_name or user.role}"[:998],
        body_text=text,
        received_at=timezone.now(),
        status=IncomingEmail.Status.PROCESSING,
    )
    return TelegramConversation.objects.create(
        chat=chat,
        requested_by=user,
        incoming_email=incoming,
        transcript=[{"role": "user", "text": text[:4000]}],
    )


def _material_question(extraction) -> tuple[str, list[tuple[str, str]]] | None:
    action = extraction.action_type
    required = {
        ParsedAction.ActionType.CREATE_CALENDAR_EVENT: ["title", "appointment_date"],
        ParsedAction.ActionType.CREATE_TASK: ["title"],
        ParsedAction.ActionType.CREATE_NOTE: ["title_or_description"],
        ParsedAction.ActionType.CREATE_EMAIL_REMINDER: [
            "title", "reminder_date", "reminder_time", "reminder_recipient"
        ],
        ParsedAction.ActionType.MARK_TASK_COMPLETE: ["task_search_text"],
    }

    missing = []
    if action == ParsedAction.ActionType.CREATE_CALENDAR_EVENT:
        if not extraction.title:
            missing.append("title")
        if not extraction.appointment_date:
            missing.append("appointment_date")
        if not extraction.all_day and not extraction.start_time:
            missing.append("start_time")
    elif action == ParsedAction.ActionType.CREATE_TASK and not extraction.title:
        missing.append("title")
    elif action == ParsedAction.ActionType.CREATE_NOTE and not (
        extraction.title or extraction.description
    ):
        missing.append("title_or_description")
    elif action == ParsedAction.ActionType.CREATE_EMAIL_REMINDER:
        for field in required[action]:
            if not getattr(extraction, field, None):
                missing.append(field)
    elif action == ParsedAction.ActionType.MARK_TASK_COMPLETE and not extraction.task_search_text:
        missing.append("task_search_text")

    material_names = set(required.get(action, [])) | {"start_time", "assigned_to", "due_date", "due_time"}
    for field in extraction.missing_fields:
        if field in material_names and field not in missing:
            missing.append(field)

    field = missing[0] if missing else None
    questions = {
        "title": ("What should I call it?", []),
        "title_or_description": ("What would you like the note to say?", []),
        "appointment_date": (
            "What date is it?",
            [("Today", "today"), ("Tomorrow", "tomorrow")],
        ),
        "start_time": (
            "What time should it start? You can also make it an all-day event.",
            [("09:00", "09:00"), ("12:00", "12:00"), ("18:00", "18:00"), ("All day", "all day")],
        ),
        "reminder_date": (
            "What date should I remind you?",
            [("Today", "today"), ("Tomorrow", "tomorrow")],
        ),
        "reminder_time": (
            "What time should I send the reminder?",
            [("09:00", "09:00"), ("12:00", "12:00"), ("18:00", "18:00")],
        ),
        "reminder_recipient": (
            "Who should receive the reminder email?",
            [("Me", "me"), ("My wife", "wife"), ("Both", "both")],
        ),
        "assigned_to": (
            "Who is this task for?",
            [("Me", "me"), ("My wife", "wife"), ("Both", "both"), ("Unassigned", "unassigned")],
        ),
        "due_date": ("When is it due? You can say “no due date”.", [("No due date", "no due date")]),
        "due_time": ("What time is it due? You can say “no specific time”.", [("No time", "no specific time")]),
        "task_search_text": ("Which open task should I mark complete?", []),
    }
    if field:
        return questions.get(field, (f"Could you clarify {field.replace('_', ' ')}?", []))

    if action == ParsedAction.ActionType.REQUIRES_REVIEW or extraction.ambiguity_notes:
        detail = extraction.ambiguity_notes[0] if extraction.ambiguity_notes else "I could not determine the request."
        return (f"I need one detail before I can continue: {detail}", [])
    return None


def _question_keyboard(conversation_id: int, options: list[tuple[str, str]]) -> dict:
    rows = [[_button(label, f"tg:a:{conversation_id}:{value}")] for label, value in options]
    rows.append([_button("Cancel", f"tg:cancel:{conversation_id}")])
    rows.extend(_navigation_rows())
    return _keyboard(*rows)


def _summary(extraction) -> str:
    esc = lambda value: html.escape(str(value)) if value not in (None, "") else "—"
    labels = {
        ParsedAction.ActionType.CREATE_CALENDAR_EVENT: "📅 <b>Calendar event</b>",
        ParsedAction.ActionType.CREATE_TASK: "✅ <b>Task</b>",
        ParsedAction.ActionType.CREATE_NOTE: "📝 <b>Note</b>",
        ParsedAction.ActionType.CREATE_EMAIL_REMINDER: "⏰ <b>Email reminder</b>",
        ParsedAction.ActionType.MARK_TASK_COMPLETE: "☑️ <b>Complete task</b>",
    }
    lines = [labels.get(extraction.action_type, "<b>LifeOS action</b>")]
    if extraction.action_type == ParsedAction.ActionType.CREATE_CALENDAR_EVENT:
        when = extraction.appointment_date
        if extraction.all_day:
            when = f"{when} · all day"
        elif extraction.start_time:
            when = f"{when} · {extraction.start_time}"
            if extraction.end_time:
                when += f"–{extraction.end_time}"
        lines += [f"<b>{esc(extraction.title)}</b>", esc(when)]
        if extraction.location:
            lines.append(f"📍 {esc(extraction.location)}")
    elif extraction.action_type == ParsedAction.ActionType.CREATE_TASK:
        lines += [f"<b>{esc(extraction.title)}</b>", f"For: {esc(extraction.assigned_to or 'unassigned')}"]
        if extraction.due_date:
            lines.append(f"Due: {esc(extraction.due_date)} {esc(extraction.due_time) if extraction.due_time else ''}".rstrip())
    elif extraction.action_type == ParsedAction.ActionType.CREATE_NOTE:
        lines += [f"<b>{esc(extraction.title or 'Note')}</b>", esc(extraction.description)]
    elif extraction.action_type == ParsedAction.ActionType.CREATE_EMAIL_REMINDER:
        lines += [
            f"<b>{esc(extraction.title)}</b>",
            f"When: {esc(extraction.reminder_date)} · {esc(extraction.reminder_time)}",
            f"For: {esc(extraction.reminder_recipient)}",
        ]
    elif extraction.action_type == ParsedAction.ActionType.MARK_TASK_COMPLETE:
        lines.append(f"Task matching: <b>{esc(extraction.task_search_text)}</b>")
    if extraction.confidence < settings.AUTOMATIC_ACTION_CONFIDENCE_THRESHOLD:
        lines.append("\n<i>I’m not completely certain, so please check this carefully.</i>")
    lines.append("\nIs this right?")
    return "\n".join(lines)


def _confirmation_keyboard(conversation_id: int) -> dict:
    return _keyboard(
        [_button("Confirm", f"tg:confirm:{conversation_id}"), _button("Edit", f"tg:edit:{conversation_id}")],
        [_button("Cancel", f"tg:cancel:{conversation_id}")],
        *_navigation_rows(),
    )


def _update_incoming_body(conversation: TelegramConversation) -> None:
    conversation.incoming_email.body_text = "\n".join(
        f"{item.get('role', 'user')}: {item.get('text', '')}" for item in conversation.transcript
    )[:20000]
    conversation.incoming_email.save(update_fields=["body_text", "updated_at"])


def _extract_and_respond(conversation: TelegramConversation, bot: TelegramBot) -> None:
    extraction = extract_telegram_action(
        conversation.transcript,
        current_date=timezone.localdate(),
        sender_role=conversation.requested_by.role,
    )
    conversation.draft_extraction = extraction.model_dump(mode="json")

    if extraction.action_type == ParsedAction.ActionType.UNSUPPORTED:
        conversation.status = TelegramConversation.Status.COMPLETED
        conversation.completed_at = timezone.now()
        conversation.incoming_email.status = IncomingEmail.Status.PROCESSED
        conversation.incoming_email.save(update_fields=["status", "updated_at"])
        conversation.save(update_fields=["draft_extraction", "status", "completed_at", "updated_at"])
        bot.send_message(
            conversation.chat.chat_id,
            "I can currently help with calendar events, tasks, notes, reminders, and completing tasks.",
            reply_markup=main_menu(),
        )
        return

    question = _material_question(extraction)
    if question and conversation.clarification_count < settings.TELEGRAM_MAX_CLARIFICATIONS:
        prompt, options = question
        conversation.status = TelegramConversation.Status.AWAITING_CLARIFICATION
        conversation.clarification_count += 1
        conversation.transcript = list(conversation.transcript) + [
            {"role": "assistant", "text": prompt[:1000]}
        ]
        conversation.save(
            update_fields=[
                "draft_extraction", "status", "clarification_count", "transcript", "updated_at"
            ]
        )
        _update_incoming_body(conversation)
        sent = bot.send_message(
            conversation.chat.chat_id,
            html.escape(prompt),
            reply_markup=_question_keyboard(conversation.id, options),
        )
        conversation.last_bot_message_id = sent.get("message_id")
        conversation.save(update_fields=["last_bot_message_id", "updated_at"])
        return

    if question:
        parsed = build_parsed_action_from_extraction(conversation.incoming_email, extraction)
        parsed.status = ParsedAction.Status.PENDING_REVIEW
        parsed.ambiguity_notes = list(parsed.ambiguity_notes) + [
            "Telegram clarification limit reached."
        ]
        parsed.save(update_fields=["status", "ambiguity_notes"])
        conversation.parsed_action = parsed
        conversation.status = TelegramConversation.Status.COMPLETED
        conversation.completed_at = timezone.now()
        conversation.incoming_email.status = IncomingEmail.Status.PENDING_REVIEW
        conversation.incoming_email.save(update_fields=["status", "updated_at"])
        conversation.save(
            update_fields=["draft_extraction", "parsed_action", "status", "completed_at", "updated_at"]
        )
        bot.send_message(
            conversation.chat.chat_id,
            "I still couldn’t resolve that safely, so I placed it in the LifeOS Review queue.",
            reply_markup=main_menu(),
        )
        return


    parsed = build_parsed_action_from_extraction(conversation.incoming_email, extraction)
    conversation.parsed_action = parsed
    conversation.status = TelegramConversation.Status.AWAITING_CONFIRMATION
    conversation.save(
        update_fields=["draft_extraction", "parsed_action", "status", "updated_at"]
    )
    sent = bot.send_message(
        conversation.chat.chat_id,
        _summary(extraction),
        reply_markup=_confirmation_keyboard(conversation.id),
    )
    conversation.last_bot_message_id = sent.get("message_id")
    conversation.save(update_fields=["last_bot_message_id", "updated_at"])


def _handle_text(
    *, chat: TelegramChat, user: TelegramUser, text: str, update_id: int, bot: TelegramBot
) -> None:
    inbox_action = TelegramInboxAction.objects.filter(
        chat=chat,
        requested_by=user,
        status=TelegramInboxAction.Status.AWAITING_INPUT,
    ).order_by("-created_at").first()
    if inbox_action:
        _handle_inbox_input(inbox_action, text, bot)
        return
    active = (
        TelegramConversation.objects.filter(
            chat=chat, requested_by=user, status__in=ACTIVE_STATUSES
        )
        .order_by("-created_at")
        .first()
    )
    if active and active.status == TelegramConversation.Status.AWAITING_CONFIRMATION:
        bot.send_message(
            chat.chat_id,
            "Please use Confirm, Edit, or Cancel on the pending request first.",
            reply_markup=_confirmation_keyboard(active.id),
        )
        return
    if active:
        active.transcript = list(active.transcript) + [{"role": "user", "text": text[:4000]}]
        active.save(update_fields=["transcript", "updated_at"])
        _update_incoming_body(active)
        _extract_and_respond(active, bot)
        return
    normalised = " ".join(text.lower().split())
    if re.search(r"\b(show|list|what|which)\b.*\b(tasks?|to[- ]?do)\b", normalised):
        _list_tasks(chat.chat_id, bot)
        return
    if re.search(r"\b(show|list|find|search)\b.*\b(notes?|reminders?)\b", normalised):
        if "reminder" in normalised:
            _list_reminders(chat.chat_id, bot)
        else:
            _list_notes(chat.chat_id, bot)
        return
    if re.search(
        r"\b(show|list|what|anything)\b.*\b(upcoming|calendar|today|tomorrow|week|fortnight)\b",
        normalised,
    ):
        _list_upcoming(chat.chat_id, bot)
        return
    conversation = _new_conversation(chat, user, text, update_id)
    _extract_and_respond(conversation, bot)


def _page_keyboard(
    kind: str,
    page: int,
    has_next: bool,
    *,
    origin: str = "home",
    back: str | None = None,
) -> list[list[dict]]:
    row = []
    if page:
        row.append(_button("‹ Previous", f"tg:inbox:{kind}:{page - 1}:{origin}"))
    if has_next:
        row.append(_button("Next ›", f"tg:inbox:{kind}:{page + 1}:{origin}"))
    rows = [row] if row else []
    rows.extend(_navigation_rows(back or _origin_callback(origin)))
    return rows


def _list_tasks(chat_id: int, bot: TelegramBot, page: int = 0, *, origin: str = "home") -> None:
    tasks, page, has_next = open_tasks(page)
    if not tasks:
        bot.send_message(
            chat_id,
            "No open tasks. Nicely done.",
            reply_markup=_navigation_keyboard(_origin_callback(origin)),
        )
        return
    lines = ["✅ <b>Open tasks</b>"]
    rows = []
    for task in tasks:
        due = f" · due {task.due_date}" if task.due_date else ""
        lines.append(f"• {html.escape(task.title)}{due}")
        rows.append([
            _button(f"Done: {task.title[:24]}", f"tg:done:{task.id}"),
            _button("Edit", f"tg:task:edit:{task.id}:{page}:{origin}"),
        ])
    rows.extend(_page_keyboard("tasks", page, has_next, origin=origin))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=_keyboard(*rows))


def _list_upcoming(chat_id: int, bot: TelegramBot) -> None:
    today = timezone.localdate()
    through = today + timedelta(days=14)
    events = calendar_items(days=14)[:10]
    tasks = Task.objects.exclude(
        status__in=[Task.Status.COMPLETED, Task.Status.CANCELLED]
    ).filter(due_date__gte=today, due_date__lte=through).order_by("due_date", "due_time")[:10]
    lines = ["🗓 <b>Next 14 days</b>"]
    for event in events:
        when = event.start_time.strftime("%H:%M") if event.start_time else "all day"
        lines.append(f"• {event.appointment_date} {when} — {html.escape(event.title)}")
    for task in tasks:
        lines.append(f"• {task.due_date} — ✅ {html.escape(task.title)}")
    if len(lines) == 1:
        lines.append("Nothing scheduled or due.")
    bot.send_message(chat_id, "\n".join(lines), reply_markup=_navigation_keyboard())


def _list_calendar(chat_id: int, bot: TelegramBot, page: int = 0, *, origin: str = "home") -> None:
    events, page, has_next = upcoming_events(page)
    if not events:
        bot.send_message(
            chat_id,
            "No calendar events in the next 30 days.",
            reply_markup=_navigation_keyboard(_origin_callback(origin)),
        )
        return
    lines = ["📅 <b>Upcoming calendar</b>"]
    rows = []
    for offset, event in enumerate(events, start=1):
        number = page * PAGE_SIZE + offset
        when = "all day" if event.all_day or not event.start_time else event.start_time.strftime("%H:%M")
        lines.append(f"\n<b>{number}.</b> {event.appointment_date} · {when}\n{html.escape(event.title)}")
        callback_kind = "shift" if event.kind == "work_shift" else "event"
        rows.append([
            _button(
                f"{number} · {event.title[:38]}",
                f"tg:{callback_kind}:view:{event.object_id}:{page}:{origin}",
            )
        ])
    rows.extend(_page_keyboard("calendar", page, has_next, origin=origin))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=_keyboard(*rows))


def _show_calendar_event(
    chat_id: int,
    event: CalendarEventRecord,
    bot: TelegramBot,
    *,
    page: int = 0,
    origin: str = "home",
) -> None:
    if event.all_day or not event.start_time:
        when = "All day"
    elif event.end_time:
        when = f"{event.start_time:%H:%M}–{event.end_time:%H:%M}"
    else:
        when = event.start_time.strftime("%H:%M")
    lines = [
        f"📅 <b>{html.escape(event.title)}</b>",
        event.appointment_date.strftime("%A %d %B %Y"),
        when,
    ]
    if event.location:
        lines.append(f"📍 {html.escape(event.location)}")
    if event.recurrence_description:
        lines.append(f"🔁 {html.escape(event.recurrence_description)}")

    if event.recurrence_rule:
        rows = [[_button("Why read-only?", f"tg:event:info:{event.id}:{page}:{origin}")]]
    else:
        rows = [[
            _button("Reschedule", f"tg:event:reschedule:{event.id}:{page}:{origin}"),
            _button("Cancel event", f"tg:event:cancel:{event.id}:{page}:{origin}"),
        ]]
    rows.extend(
        _navigation_rows(
            f"tg:nav:calendar:{page}:{origin}",
            back_label="⬅️ Back to calendar",
        )
    )
    bot.send_message(chat_id, "\n".join(lines), reply_markup=_keyboard(*rows))


def _show_patchwork_shift(
    chat_id: int,
    shift: PatchworkShift,
    bot: TelegramBot,
    *,
    page: int = 0,
    origin: str = "home",
) -> None:
    item = calendar_item_for_shift(shift)
    if item.all_day or not item.start_time:
        when = "All day"
    elif item.end_time:
        if item.end_date and item.end_date != item.appointment_date:
            when = f"{item.start_time:%H:%M}–{item.end_date:%A} {item.end_time:%H:%M}"
        else:
            when = f"{item.start_time:%H:%M}–{item.end_time:%H:%M}"
    else:
        when = item.start_time.strftime("%H:%M")
    lines = [
        f"👨‍⚕️ <b>{html.escape(item.title)}</b>",
        item.appointment_date.strftime("%A %d %B %Y"),
        when,
        "Managed by Patchwork · read-only in Telegram",
    ]
    rows = [[
        _button("Why read-only?", f"tg:shift:info:{shift.id}:{page}:{origin}")
    ]]
    rows.extend(
        _navigation_rows(
            f"tg:nav:calendar:{page}:{origin}",
            back_label="⬅️ Back to calendar",
        )
    )
    bot.send_message(chat_id, "\n".join(lines), reply_markup=_keyboard(*rows))


def _list_notes(
    chat_id: int, bot: TelegramBot, page: int = 0, query: str = "", *, origin: str = "home"
) -> None:
    notes, page, has_next = recent_notes(page, query)
    heading = "📝 <b>Notes</b>" if not query else f"📝 <b>Notes matching {html.escape(query)}</b>"
    if not notes:
        bot.send_message(chat_id, "No notes found.", reply_markup=_navigation_keyboard(_origin_callback(origin)))
        return
    lines = [heading]
    rows = []
    for note in notes:
        category = f" · {html.escape(note.category)}" if note.category else ""
        lines.append(f"• {html.escape(note.title or 'Untitled note')}{category}")
        rows.append([
            _button(
                f"View: {(note.title or 'Untitled')[:28]}",
                f"tg:note:view:{note.id}:{page}:{origin}",
            )
        ])
    rows.extend(_page_keyboard("notes", page, has_next, origin=origin))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=_keyboard(*rows))


def _show_note(
    chat_id: int, note: Note, bot: TelegramBot, *, page: int = 0, origin: str = "home"
) -> None:
    body = html.escape(note.body or "(empty)")
    text = f"📝 <b>{html.escape(note.title or 'Untitled note')}</b>\n{body}"
    bot.send_message(
        chat_id,
        text,
        reply_markup=_keyboard(
            [_button("Edit note", f"tg:note:edit:{note.id}:{page}:{origin}")],
            *_navigation_rows(f"tg:nav:notes:{page}:{origin}", back_label="⬅️ Back to notes"),
        ),
    )


def _list_reminders(chat_id: int, bot: TelegramBot, page: int = 0, *, origin: str = "home") -> None:
    reminders, page, has_next = pending_reminders(page)
    if not reminders:
        bot.send_message(
            chat_id,
            "No pending reminders.",
            reply_markup=_navigation_keyboard(_origin_callback(origin)),
        )
        return
    lines = ["⏰ <b>Pending reminders</b>"]
    rows = []
    for reminder in reminders:
        lines.append(
            f"• {reminder.reminder_date} {reminder.reminder_time.strftime('%H:%M')} — {html.escape(reminder.title)}"
        )
        rows.append([
            _button("Snooze 1 day", f"tg:rem:snooze:{reminder.id}"),
            _button("Cancel", f"tg:rem:cancel:{reminder.id}"),
        ])
    rows.extend(_page_keyboard("reminders", page, has_next, origin=origin))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=_keyboard(*rows))


def build_today_message() -> str:
    """Public so the scheduled briefing command renders the same live view."""
    events, tasks, reminders = today_items()
    lines = ["☀️ <b>LifeOS today</b>"]
    if events:
        lines.append("\n<b>Calendar</b>")
        lines.extend(
            f"• {event.start_time.strftime('%H:%M') if event.start_time else 'all day'} — {html.escape(event.title)}"
            for event in events[:5]
        )
    if tasks:
        lines.append("\n<b>Tasks</b>")
        for task in tasks[:5]:
            prefix = "Overdue: " if task.due_date and task.due_date < timezone.localdate() else ""
            lines.append(f"• {prefix}{html.escape(task.title)}")
    if reminders:
        lines.append("\n<b>Reminders</b>")
        lines.extend(
            f"• {reminder.reminder_time.strftime('%H:%M')} — {html.escape(reminder.title)}"
            for reminder in reminders[:5]
        )
    if len(lines) == 1:
        lines.append("Nothing scheduled or due today.")
    return "\n".join(lines)


PLANNING_SECTION_LIMIT = 10


def _planning_date_label(value: date) -> str:
    return f"{value:%a} {value.day} {value:%b %Y}"


def _planning_title(value: str) -> str:
    """Escape and truncate a title while counting its rendered HTML length."""
    value = value.strip()
    escaped = html.escape(value)
    if len(escaped) <= 72:
        return escaped

    parts = []
    escaped_length = 0
    for character in value:
        escaped_character = html.escape(character)
        if escaped_length + len(escaped_character) > 71:
            break
        parts.append(escaped_character)
        escaped_length += len(escaped_character)
    return "".join(parts).rstrip() + "…"


def build_planning_message(*, period: str, start_date: date, end_date: date) -> str:
    """Render a bounded week-ahead or month-ahead planning digest."""
    if period not in {"weekly", "monthly"}:
        raise ValueError("Planning briefing period must be weekly or monthly.")
    if end_date <= start_date:
        raise ValueError("Planning briefing end date must be after its start date.")
    events, tasks, reminders = planning_items(start_date, end_date)
    period_label = "week" if period == "weekly" else "month"
    final_date = end_date - timedelta(days=1)
    lines = [
        f"🗓 <b>LifeOS {period_label} ahead</b>",
        f"<b>{_planning_date_label(start_date)} – {_planning_date_label(final_date)}</b>",
    ]

    def append_limited(items, formatter) -> None:
        lines.extend(formatter(item) for item in items[:PLANNING_SECTION_LIMIT])
        if len(items) > PLANNING_SECTION_LIMIT:
            lines.append(f"• …and {len(items) - PLANNING_SECTION_LIMIT} more")

    if events:
        lines.append("\n<b>Calendar</b>")
        append_limited(
            events,
            lambda event: (
                f"• {_planning_date_label(event.appointment_date)}, "
                f"{event.start_time.strftime('%H:%M') if event.start_time else 'all day'}"
                f" — {_planning_title(event.title)}"
            ),
        )
    if tasks:
        lines.append("\n<b>Tasks due</b>")
        append_limited(
            tasks,
            lambda task: (
                f"• {_planning_date_label(task.due_date)}"
                f"{', ' + task.due_time.strftime('%H:%M') if task.due_time else ''}"
                f" — {_planning_title(task.title)}"
            ),
        )
    if reminders:
        lines.append("\n<b>Reminders</b>")
        append_limited(
            reminders,
            lambda reminder: (
                f"• {_planning_date_label(reminder.reminder_date)}, {reminder.reminder_time:%H:%M}"
                f" — {_planning_title(reminder.title)}"
            ),
        )
    if not events and not tasks and not reminders:
        lines.append(f"Nothing scheduled for the {period_label} ahead.")
    return "\n".join(lines)


def today_keyboard() -> dict:
    return _keyboard(
        [_button("✅ Open tasks", "tg:inbox:tasks:0:today"), _button("📅 Calendar", "tg:inbox:calendar:0:today")],
        [_button("📝 Notes", "tg:inbox:notes:0:today"), _button("⏰ Reminders", "tg:inbox:reminders:0:today")],
        [_button("➕ Add task", "tg:new:task")],
        *_navigation_rows(HOME_CALLBACK),
    )


def _show_today(chat_id: int, bot: TelegramBot) -> None:
    bot.send_message(chat_id, build_today_message(), reply_markup=today_keyboard())


def _show_planning(period: str, chat_id: int, bot: TelegramBot) -> None:
    start_date = timezone.localdate()
    end_date = (
        start_date + timedelta(days=7)
        if period == "weekly"
        else one_month_after(start_date)
    )
    bot.send_message(
        chat_id,
        build_planning_message(period=period, start_date=start_date, end_date=end_date),
        reply_markup=today_keyboard(),
    )


def _cancel_active(chat: TelegramChat, user: TelegramUser, bot: TelegramBot) -> None:
    active = TelegramConversation.objects.filter(
        chat=chat, requested_by=user, status__in=ACTIVE_STATUSES
    ).order_by("-created_at").first()
    if not active:
        bot.send_message(chat.chat_id, "There is no pending request to cancel.", reply_markup=main_menu())
        return
    _cancel_conversation(active, bot)


def _cancel_conversation(conversation: TelegramConversation, bot: TelegramBot) -> None:
    if conversation.parsed_action and conversation.parsed_action.status != ParsedAction.Status.EXECUTED:
        conversation.parsed_action.status = ParsedAction.Status.REJECTED
        conversation.parsed_action.save(update_fields=["status", "updated_at"])
    conversation.status = TelegramConversation.Status.CANCELLED
    conversation.completed_at = timezone.now()
    conversation.incoming_email.status = IncomingEmail.Status.PROCESSED
    conversation.incoming_email.save(update_fields=["status", "updated_at"])
    conversation.save(update_fields=["status", "completed_at", "updated_at"])
    bot.send_message(conversation.chat.chat_id, "Cancelled.", reply_markup=main_menu())


def _cancel_pending_for_navigation(chat: TelegramChat, user: TelegramUser) -> None:
    """Abandon pending input before navigating so later text is not captured."""
    for conversation in TelegramConversation.objects.filter(
        chat=chat, requested_by=user, status__in=ACTIVE_STATUSES
    ).select_related("parsed_action", "incoming_email"):
        if conversation.parsed_action and conversation.parsed_action.status != ParsedAction.Status.EXECUTED:
            conversation.parsed_action.status = ParsedAction.Status.REJECTED
            conversation.parsed_action.save(update_fields=["status", "updated_at"])
        conversation.status = TelegramConversation.Status.CANCELLED
        conversation.completed_at = timezone.now()
        conversation.incoming_email.status = IncomingEmail.Status.PROCESSED
        conversation.incoming_email.save(update_fields=["status", "updated_at"])
        conversation.save(update_fields=["status", "completed_at", "updated_at"])

    TelegramInboxAction.objects.filter(
        chat=chat,
        requested_by=user,
        status__in=[
            TelegramInboxAction.Status.AWAITING_INPUT,
            TelegramInboxAction.Status.AWAITING_CONFIRMATION,
        ],
    ).update(status=TelegramInboxAction.Status.CANCELLED, completed_at=timezone.now())


def _preference_for(user: TelegramUser) -> TelegramPreference:
    preference, _ = TelegramPreference.objects.get_or_create(user=user)
    return preference


def _show_settings(chat: TelegramChat, user: TelegramUser, bot: TelegramBot) -> None:
    preference = _preference_for(user)
    enabled = "on" if preference.briefing_enabled else "off"
    bot.send_message(
        chat.chat_id,
        f"⚙️ <b>Daily briefing</b>\nStatus: <b>{enabled}</b>\nTime: <b>{preference.briefing_time:%H:%M}</b>",
        reply_markup=_keyboard(
            [_button("Turn off" if preference.briefing_enabled else "Turn on", "tg:settings:toggle")],
            [_button("Change time", "tg:settings:time")],
            *_navigation_rows(),
        ),
    )


def _start_inbox_action(
    chat: TelegramChat,
    user: TelegramUser,
    action: str,
    *,
    object_id: int | None = None,
    return_callback: str = HOME_CALLBACK,
) -> TelegramInboxAction:
    TelegramInboxAction.objects.filter(
        chat=chat,
        requested_by=user,
        status__in=[TelegramInboxAction.Status.AWAITING_INPUT, TelegramInboxAction.Status.AWAITING_CONFIRMATION],
    ).update(status=TelegramInboxAction.Status.CANCELLED, completed_at=timezone.now())
    return TelegramInboxAction.objects.create(
        chat=chat,
        requested_by=user,
        action=action,
        object_id=object_id,
        proposed_data={"return_callback": return_callback},
    )


def _action_keyboard(inbox_action: TelegramInboxAction, *, back_callback: str = HOME_CALLBACK) -> dict:
    return _keyboard(
        [_button("Confirm", f"tg:ia:confirm:{inbox_action.id}"), _button("Cancel", f"tg:ia:cancel:{inbox_action.id}")],
        *_navigation_rows(back_callback),
    )


def _parse_task_edit(value: str, task: Task) -> dict:
    """Parse a deliberately explicit Telegram task-edit format without an LLM."""
    parts = [part.strip() for part in value.split("|")]
    title = parts[0] if parts else ""
    if not title:
        raise ValueError("Start with the task title.")
    due_date = task.due_date
    assigned_to = task.assigned_to
    if len(parts) > 1 and parts[1]:
        due_date = None if parts[1].lower() in {"none", "no due date"} else date.fromisoformat(parts[1])
    if len(parts) > 2 and parts[2]:
        assigned_to = parts[2].lower()
    if len(parts) > 3:
        raise ValueError("Use: title | YYYY-MM-DD or none | ike/wife/both/unassigned")
    return {"title": title, "due_date": due_date.isoformat() if due_date else "", "assigned_to": assigned_to}


def _handle_inbox_input(inbox_action: TelegramInboxAction, text: str, bot: TelegramBot) -> None:
    return_callback = inbox_action.proposed_data.get("return_callback", HOME_CALLBACK)
    try:
        if inbox_action.action == TelegramInboxAction.Action.EDIT_NOTE:
            note = Note.objects.get(pk=inbox_action.object_id)
            body = text.strip()
            if not body:
                raise ValueError("A note cannot be empty.")
            inbox_action.proposed_data = {"body": body, "return_callback": return_callback}
            prompt = f"Replace <b>{html.escape(note.title or 'this note')}</b> with:\n{html.escape(body)}\n\nIs this right?"
        elif inbox_action.action == TelegramInboxAction.Action.EDIT_TASK:
            task = Task.objects.get(pk=inbox_action.object_id)
            inbox_action.proposed_data = {
                **_parse_task_edit(text, task),
                "return_callback": return_callback,
            }
            data = inbox_action.proposed_data
            due = data["due_date"] or "no due date"
            prompt = (
                f"Update task to <b>{html.escape(data['title'])}</b>\n"
                f"Due: {due}\nFor: {html.escape(data['assigned_to'])}\n\nIs this right?"
            )
        elif inbox_action.action == TelegramInboxAction.Action.RESCHEDULE_EVENT:
            raw = text.strip()
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M")
                appointment_date, start_time = parsed.date(), parsed.time()
            except ValueError:
                appointment_date, start_time = date.fromisoformat(raw), None
            if appointment_date < timezone.localdate():
                raise ValueError("Choose today or a future date.")
            event = CalendarEventRecord.objects.get(pk=inbox_action.object_id)
            inbox_action.proposed_data = {
                "appointment_date": appointment_date.isoformat(),
                "start_time": start_time.strftime("%H:%M") if start_time else "",
                "return_callback": return_callback,
            }
            when = appointment_date.isoformat() + (f" at {start_time:%H:%M}" if start_time else " (all day)")
            prompt = f"Reschedule <b>{html.escape(event.title)}</b> to <b>{when}</b>?"
        elif inbox_action.action == TelegramInboxAction.Action.SET_BRIEFING_TIME:
            parsed_time = datetime.strptime(text.strip(), "%H:%M").time()
            inbox_action.proposed_data = {
                "briefing_time": parsed_time.strftime("%H:%M"),
                "return_callback": return_callback,
            }
            prompt = f"Send your daily briefing at <b>{parsed_time:%H:%M}</b>?"
        else:
            raise ValueError("That inbox action is no longer supported.")
    except (Note.DoesNotExist, Task.DoesNotExist, CalendarEventRecord.DoesNotExist):
        inbox_action.status = TelegramInboxAction.Status.CANCELLED
        inbox_action.completed_at = timezone.now()
        inbox_action.save(update_fields=["status", "completed_at", "updated_at"])
        bot.send_message(
            inbox_action.chat.chat_id,
            "That item is no longer available.",
            reply_markup=_navigation_keyboard(return_callback),
        )
        return
    except ValueError as exc:
        bot.send_message(
            inbox_action.chat.chat_id,
            f"{html.escape(str(exc))}\nPlease try again.",
            reply_markup=_navigation_keyboard(return_callback),
        )
        return

    inbox_action.status = TelegramInboxAction.Status.AWAITING_CONFIRMATION
    inbox_action.save(update_fields=["proposed_data", "status", "updated_at"])
    bot.send_message(
        inbox_action.chat.chat_id,
        prompt,
        reply_markup=_action_keyboard(inbox_action, back_callback=return_callback),
    )


def _confirm_inbox_action(inbox_action: TelegramInboxAction, bot: TelegramBot) -> None:
    if inbox_action.status != TelegramInboxAction.Status.AWAITING_CONFIRMATION:
        bot.send_message(
            inbox_action.chat.chat_id,
            "That change has already been handled.",
            reply_markup=_navigation_keyboard(
                inbox_action.proposed_data.get("return_callback", HOME_CALLBACK)
            ),
        )
        return
    data = inbox_action.proposed_data
    try:
        if inbox_action.action == TelegramInboxAction.Action.EDIT_NOTE:
            note = update_note_body(Note.objects.get(pk=inbox_action.object_id), data["body"])
            message = f"📝 Updated <b>{html.escape(note.title or 'note')}</b>."
        elif inbox_action.action == TelegramInboxAction.Action.EDIT_TASK:
            task = update_task(
                Task.objects.get(pk=inbox_action.object_id),
                title=data["title"],
                due_date=date.fromisoformat(data["due_date"]) if data["due_date"] else None,
                assigned_to=data["assigned_to"],
            )
            message = f"✅ Updated <b>{html.escape(task.title)}</b>."
        elif inbox_action.action == TelegramInboxAction.Action.RESCHEDULE_EVENT:
            event = reschedule_calendar_event(
                CalendarEventRecord.objects.get(pk=inbox_action.object_id),
                appointment_date=date.fromisoformat(data["appointment_date"]),
                start_time=datetime.strptime(data["start_time"], "%H:%M").time() if data["start_time"] else None,
            )
            message = f"📅 Rescheduled <b>{html.escape(event.title)}</b>."
        elif inbox_action.action == TelegramInboxAction.Action.SET_BRIEFING_TIME:
            preference = _preference_for(inbox_action.requested_by)
            preference.briefing_time = datetime.strptime(data["briefing_time"], "%H:%M").time()
            preference.save(update_fields=["briefing_time", "updated_at"])
            message = f"☀️ Daily briefing time set to <b>{preference.briefing_time:%H:%M}</b>."
        else:
            raise ValueError("That inbox action is no longer supported.")
    except (Note.DoesNotExist, Task.DoesNotExist, CalendarEventRecord.DoesNotExist):
        message = "That item is no longer available. No change was made."
    except (KeyError, ValueError) as exc:
        message = f"I couldn’t make that change safely: {html.escape(str(exc))}"
    else:
        inbox_action.status = TelegramInboxAction.Status.COMPLETED
        inbox_action.completed_at = timezone.now()
        inbox_action.save(update_fields=["status", "completed_at", "updated_at"])
    bot.send_message(inbox_action.chat.chat_id, message, reply_markup=main_menu())


def _outcome_text(outcome: str, extra: dict) -> str:
    if outcome in ("created", "created_with_reminder", "created_reminder_unclear"):
        obj = extra.get("calendar_event")
        return f"📅 Added <b>{html.escape(obj.title)}</b> to the calendar." if obj else "📅 Calendar event handled."
    if outcome == "task_created":
        return f"✅ Created task <b>{html.escape(extra['task'].title)}</b>."
    if outcome == "note_created":
        return f"📝 Saved note <b>{html.escape(extra['note'].title)}</b>."
    if outcome == "reminder_created":
        return f"⏰ Created reminder <b>{html.escape(extra['reminder'].title)}</b>."
    if outcome == "task_completed":
        return f"☑️ Completed <b>{html.escape(extra['task'].title)}</b>."
    if outcome == "task_not_found":
        return "I couldn’t find a matching open task."
    return "That request now needs a quick check in the LifeOS Review queue."


def _confirm_conversation(conversation: TelegramConversation, bot: TelegramBot) -> None:
    with transaction.atomic():
        locked = TelegramConversation.objects.select_for_update().get(pk=conversation.pk)
        if locked.status != TelegramConversation.Status.AWAITING_CONFIRMATION:
            bot.send_message(
                locked.chat.chat_id,
                "That request has already been handled.",
                reply_markup=_navigation_keyboard(),
            )
            return
        locked.status = TelegramConversation.Status.EXECUTING
        locked.save(update_fields=["status", "updated_at"])

    try:
        outcome, extra = admin_approve_and_execute(locked.parsed_action)
    except Exception as exc:  # noqa: BLE001 - record safely and avoid uncertain automatic retry
        locked.status = TelegramConversation.Status.FAILED
        locked.last_error = str(exc)[:2000]
        locked.incoming_email.status = IncomingEmail.Status.FAILED
        locked.incoming_email.last_error = str(exc)[:2000]
        locked.incoming_email.save(update_fields=["status", "last_error", "updated_at"])
        locked.save(update_fields=["status", "last_error", "updated_at"])
        logger.exception("Telegram conversation execution failed conversation_id=%s", locked.id)
        bot.send_message(
            locked.chat.chat_id,
            "I couldn’t finish that safely. Nothing will be retried automatically; please check LifeOS Review.",
            reply_markup=main_menu(),
        )
        return

    locked.status = TelegramConversation.Status.COMPLETED
    locked.completed_at = timezone.now()
    locked.transcript = []
    locked.incoming_email.status = (
        IncomingEmail.Status.PENDING_REVIEW
        if outcome == "pending_review"
        else IncomingEmail.Status.PROCESSED
    )
    locked.incoming_email.save(update_fields=["status", "updated_at"])
    locked.save(update_fields=["status", "completed_at", "transcript", "updated_at"])
    bot.send_message(locked.chat.chat_id, _outcome_text(outcome, extra), reply_markup=main_menu())


def _handle_command(
    command: str, *, command_text: str = "", chat: TelegramChat, user: TelegramUser, bot: TelegramBot
) -> bool:
    command = command.split("@", 1)[0].lower()
    if command == "/linkgroup":
        if chat.chat_type not in (TelegramChat.ChatType.GROUP, TelegramChat.ChatType.SUPERGROUP):
            bot.send_message(chat.chat_id, "Use /linkgroup inside the shared family group.")
        elif user.role != HouseholdMember.Role.IKE:
            bot.send_message(chat.chat_id, "Only the configured owner can link a group.")
        else:
            chat.authorised = True
            chat.save(update_fields=["authorised", "updated_at"])
            bot.send_message(chat.chat_id, "This group is now linked to LifeOS.", reply_markup=main_menu())
        return True
    if not chat.authorised:
        return True
    if command in ("/start", "/help"):
        bot.send_message(
            chat.chat_id,
            "Hi, I’m LifeOS. Send a voice note or text. I’ll clarify anything unclear and ask for "
            "confirmation before making changes.",
            reply_markup=main_menu(),
        )
        return True
    if command == "/tasks":
        _list_tasks(chat.chat_id, bot)
        return True
    if command == "/today":
        _show_today(chat.chat_id, bot)
        return True
    if command == "/week":
        _show_planning("weekly", chat.chat_id, bot)
        return True
    if command == "/month":
        _show_planning("monthly", chat.chat_id, bot)
        return True
    if command == "/calendar":
        _list_calendar(chat.chat_id, bot)
        return True
    if command == "/notes":
        _list_notes(chat.chat_id, bot)
        return True
    if command == "/reminders":
        _list_reminders(chat.chat_id, bot)
        return True
    if command == "/settings":
        _show_settings(chat, user, bot)
        return True
    if command == "/search":
        query = command_text.split(maxsplit=1)[1].strip() if len(command_text.split(maxsplit=1)) > 1 else ""
        _search_lifeos(chat.chat_id, query, bot)
        return True
    if command == "/upcoming":
        _list_upcoming(chat.chat_id, bot)
        return True
    if command == "/cancel":
        _cancel_active(chat, user, bot)
        return True
    return False


def _search_lifeos(chat_id: int, query: str, bot: TelegramBot) -> None:
    if not query:
        bot.send_message(
            chat_id,
            "Use <b>/search words</b> to search open tasks and notes.",
            reply_markup=_navigation_keyboard(),
        )
        return
    tasks, notes = search_lifeos(query)
    if not tasks and not notes:
        bot.send_message(
            chat_id,
            f"No open tasks or notes match <b>{html.escape(query)}</b>.",
            reply_markup=_navigation_keyboard(),
        )
        return
    lines = [f"🔎 <b>Results for {html.escape(query)}</b>"]
    rows = []
    if tasks:
        lines.append("\n<b>Tasks</b>")
        for task in tasks:
            lines.append(f"• ✅ {html.escape(task.title)}")
            rows.append([_button(f"Done: {task.title[:24]}", f"tg:done:{task.id}")])
    if notes:
        lines.append("\n<b>Notes</b>")
        for note in notes:
            lines.append(f"• 📝 {html.escape(note.title or 'Untitled note')}")
            rows.append([_button(f"View: {(note.title or 'Untitled')[:24]}", f"tg:note:view:{note.id}")])
    rows.extend(_navigation_rows())
    bot.send_message(chat_id, "\n".join(lines), reply_markup=_keyboard(*rows))


def _handle_navigation(
    parts: list[str], chat: TelegramChat, user: TelegramUser, bot: TelegramBot
) -> None:
    if len(parts) < 3:
        return
    _cancel_pending_for_navigation(chat, user)
    target = parts[2]
    if target == "home":
        bot.send_message(chat.chat_id, MAIN_MENU_PROMPT, reply_markup=main_menu())
        return
    if target == "today":
        _show_today(chat.chat_id, bot)
        return
    if target == "settings":
        _show_settings(chat, user, bot)
        return

    page = 0
    if len(parts) > 3:
        try:
            page = max(int(parts[3]), 0)
        except ValueError:
            page = 0
    origin = parts[4] if len(parts) > 4 and parts[4] in {"home", "today"} else "home"
    if target == "tasks":
        _list_tasks(chat.chat_id, bot, page, origin=origin)
    elif target == "calendar":
        _list_calendar(chat.chat_id, bot, page, origin=origin)
    elif target == "notes":
        _list_notes(chat.chat_id, bot, page, origin=origin)
    elif target == "reminders":
        _list_reminders(chat.chat_id, bot, page, origin=origin)
    elif target == "event" and len(parts) > 3:
        try:
            event_id = int(parts[3])
            event_page = max(int(parts[4]), 0) if len(parts) > 4 else 0
        except ValueError:
            return
        event_origin = parts[5] if len(parts) > 5 and parts[5] in {"home", "today"} else "home"
        event = CalendarEventRecord.objects.filter(pk=event_id).first()
        if event:
            _show_calendar_event(
                chat.chat_id, event, bot, page=event_page, origin=event_origin
            )
        else:
            bot.send_message(
                chat.chat_id,
                "That calendar event is no longer available.",
                reply_markup=_navigation_keyboard("tg:nav:calendar:0:home"),
            )
    elif target == "shift" and len(parts) > 3:
        try:
            shift_id = int(parts[3])
            shift_page = max(int(parts[4]), 0) if len(parts) > 4 else 0
        except ValueError:
            return
        shift_origin = parts[5] if len(parts) > 5 and parts[5] in {"home", "today"} else "home"
        shift = PatchworkShift.objects.filter(
            pk=shift_id, active=True, suppressed=False
        ).first()
        if shift:
            _show_patchwork_shift(
                chat.chat_id, shift, bot, page=shift_page, origin=shift_origin
            )
        else:
            bot.send_message(
                chat.chat_id,
                "That Patchwork shift is no longer available.",
                reply_markup=_navigation_keyboard("tg:nav:calendar:0:home"),
            )
    elif target == "note" and len(parts) > 3:
        try:
            note_id = int(parts[3])
            note_page = max(int(parts[4]), 0) if len(parts) > 4 else 0
        except ValueError:
            return
        note_origin = parts[5] if len(parts) > 5 and parts[5] in {"home", "today"} else "home"
        note = Note.objects.filter(pk=note_id).first()
        if note:
            _show_note(chat.chat_id, note, bot, page=note_page, origin=note_origin)
        else:
            bot.send_message(
                chat.chat_id,
                "That note is no longer available.",
                reply_markup=_navigation_keyboard("tg:nav:notes:0:home"),
            )


def _handle_callback(data: str, chat: TelegramChat, user: TelegramUser, bot: TelegramBot) -> None:
    callback_parts = data.split(":")
    parts = data.split(":", 3)
    if len(parts) < 3 or parts[0] != "tg":
        return
    action = parts[1]
    target = parts[2]
    if action == "nav":
        _handle_navigation(callback_parts, chat, user, bot)
        return
    if action == "menu":
        bot.send_message(chat.chat_id, MAIN_MENU_PROMPT, reply_markup=main_menu())
        return
    if action == "today":
        _show_today(chat.chat_id, bot)
        return
    if action == "plan" and target in {"weekly", "monthly"}:
        _show_planning(target, chat.chat_id, bot)
        return
    if action == "inbox" and len(callback_parts) >= 4:
        try:
            page = max(int(callback_parts[3]), 0)
        except ValueError:
            return
        origin = (
            callback_parts[4]
            if len(callback_parts) > 4 and callback_parts[4] in {"home", "today"}
            else "home"
        )
        if target == "tasks":
            _list_tasks(chat.chat_id, bot, page, origin=origin)
        elif target == "calendar":
            _list_calendar(chat.chat_id, bot, page, origin=origin)
        elif target == "notes":
            _list_notes(chat.chat_id, bot, page, origin=origin)
        elif target == "reminders":
            _list_reminders(chat.chat_id, bot, page, origin=origin)
        return
    if action == "settings":
        if target == "show":
            _show_settings(chat, user, bot)
        elif target == "toggle":
            preference = _preference_for(user)
            preference.briefing_enabled = not preference.briefing_enabled
            preference.save(update_fields=["briefing_enabled", "updated_at"])
            _show_settings(chat, user, bot)
        elif target == "time":
            _start_inbox_action(
                chat,
                user,
                TelegramInboxAction.Action.SET_BRIEFING_TIME,
                return_callback="tg:nav:settings",
            )
            bot.send_message(
                chat.chat_id,
                "What time should I send your daily briefing? Use <b>HH:MM</b>, for example <b>07:30</b>.",
                reply_markup=_navigation_keyboard("tg:nav:settings"),
            )
        return
    if action == "note" and len(callback_parts) >= 4:
        try:
            note = Note.objects.get(pk=int(callback_parts[3]))
        except (ValueError, Note.DoesNotExist):
            bot.send_message(
                chat.chat_id,
                "That note is no longer available.",
                reply_markup=_navigation_keyboard("tg:nav:notes:0:home"),
            )
            return
        try:
            page = max(int(callback_parts[4]), 0) if len(callback_parts) > 4 else 0
        except ValueError:
            page = 0
        origin = (
            callback_parts[5]
            if len(callback_parts) > 5 and callback_parts[5] in {"home", "today"}
            else "home"
        )
        if target == "view":
            _show_note(chat.chat_id, note, bot, page=page, origin=origin)
        elif target == "edit":
            _start_inbox_action(
                chat,
                user,
                TelegramInboxAction.Action.EDIT_NOTE,
                object_id=note.id,
                return_callback=f"tg:nav:note:{note.id}:{page}:{origin}",
            )
            bot.send_message(
                chat.chat_id,
                f"Send the replacement text for <b>{html.escape(note.title or 'this note')}</b>.",
                reply_markup=_navigation_keyboard(
                    f"tg:nav:note:{note.id}:{page}:{origin}",
                    back_label="⬅️ Back to note",
                ),
            )
        return
    if action == "task" and target == "edit" and len(callback_parts) >= 4:
        try:
            task = Task.objects.get(pk=int(callback_parts[3]))
        except (ValueError, Task.DoesNotExist):
            bot.send_message(
                chat.chat_id,
                "That task is no longer available.",
                reply_markup=_navigation_keyboard("tg:nav:tasks:0:home"),
            )
            return
        try:
            page = max(int(callback_parts[4]), 0) if len(callback_parts) > 4 else 0
        except ValueError:
            page = 0
        origin = (
            callback_parts[5]
            if len(callback_parts) > 5 and callback_parts[5] in {"home", "today"}
            else "home"
        )
        _start_inbox_action(
            chat,
            user,
            TelegramInboxAction.Action.EDIT_TASK,
            object_id=task.id,
            return_callback=f"tg:nav:tasks:{page}:{origin}",
        )
        due = task.due_date.isoformat() if task.due_date else "none"
        bot.send_message(
            chat.chat_id,
            "Send: <b>title | YYYY-MM-DD or none | ike/wife/both/unassigned</b>\n"
            f"Current: <b>{html.escape(task.title)}</b> | {due} | {html.escape(task.assigned_to)}",
            reply_markup=_navigation_keyboard(f"tg:nav:tasks:{page}:{origin}"),
        )
        return
    if action == "rem" and len(parts) == 4:
        try:
            reminder = Reminder.objects.get(pk=int(parts[3]))
            if target == "snooze":
                snooze_reminder(reminder)
                bot.send_message(chat.chat_id, f"⏰ Snoozed <b>{html.escape(reminder.title)}</b> by one day.", reply_markup=main_menu())
            elif target == "cancel":
                cancel_reminder(reminder)
                bot.send_message(chat.chat_id, f"⏰ Cancelled <b>{html.escape(reminder.title)}</b>.", reply_markup=main_menu())
        except (ValueError, Reminder.DoesNotExist) as exc:
            bot.send_message(chat.chat_id, html.escape(str(exc) or "That reminder is no longer available."), reply_markup=main_menu())
        return
    if action == "event" and len(callback_parts) >= 4:
        try:
            event = CalendarEventRecord.objects.get(pk=int(callback_parts[3]))
        except (ValueError, CalendarEventRecord.DoesNotExist):
            bot.send_message(
                chat.chat_id,
                "That calendar event is no longer available.",
                reply_markup=_navigation_keyboard("tg:nav:calendar:0:home"),
            )
            return
        try:
            page = max(int(callback_parts[4]), 0) if len(callback_parts) > 4 else 0
        except ValueError:
            page = 0
        origin = (
            callback_parts[5]
            if len(callback_parts) > 5 and callback_parts[5] in {"home", "today"}
            else "home"
        )
        event_back = f"tg:nav:event:{event.id}:{page}:{origin}"
        if target == "view":
            _show_calendar_event(chat.chat_id, event, bot, page=page, origin=origin)
            return
        if target == "reschedule":
            if event.recurrence_rule:
                bot.send_message(
                    chat.chat_id,
                    "For a recurring event, use Google Calendar so you can choose whether to change one occurrence or the whole series.",
                    reply_markup=_navigation_keyboard(event_back, back_label="⬅️ Back to event"),
                )
            else:
                _start_inbox_action(
                    chat,
                    user,
                    TelegramInboxAction.Action.RESCHEDULE_EVENT,
                    object_id=event.id,
                    return_callback=event_back,
                )
                bot.send_message(
                    chat.chat_id,
                    "Send the new date/time as <b>YYYY-MM-DD HH:MM</b>, for example <b>2026-08-20 14:30</b>. Send just <b>YYYY-MM-DD</b> for an all-day event.",
                    reply_markup=_navigation_keyboard(event_back, back_label="⬅️ Back to event"),
                )
        elif target == "cancel":
            if event.recurrence_rule:
                bot.send_message(
                    chat.chat_id,
                    "For a recurring event, use Google Calendar so you can choose whether to cancel one occurrence or the whole series.",
                    reply_markup=_navigation_keyboard(event_back, back_label="⬅️ Back to event"),
                )
            else:
                inbox_action = _start_inbox_action(
                    chat,
                    user,
                    TelegramInboxAction.Action.CANCEL_EVENT,
                    object_id=event.id,
                    return_callback=event_back,
                )
                inbox_action.proposed_data = {"cancel": True, "return_callback": event_back}
                inbox_action.status = TelegramInboxAction.Status.AWAITING_CONFIRMATION
                inbox_action.save(update_fields=["proposed_data", "status", "updated_at"])
                bot.send_message(
                    chat.chat_id,
                    f"Cancel <b>{html.escape(event.title)}</b> from Google Calendar?",
                    reply_markup=_action_keyboard(inbox_action, back_callback=event_back),
                )
        elif target == "info":
            bot.send_message(
                chat.chat_id,
                "Recurring events are read-only in Telegram for now. Use Google Calendar to change one occurrence or the whole series.",
                reply_markup=_navigation_keyboard(event_back, back_label="⬅️ Back to event"),
            )
        return
    if action == "shift" and len(callback_parts) >= 4:
        try:
            shift = PatchworkShift.objects.get(
                pk=int(callback_parts[3]), active=True, suppressed=False
            )
        except (ValueError, PatchworkShift.DoesNotExist):
            bot.send_message(
                chat.chat_id,
                "That Patchwork shift is no longer available.",
                reply_markup=_navigation_keyboard("tg:nav:calendar:0:home"),
            )
            return
        try:
            page = max(int(callback_parts[4]), 0) if len(callback_parts) > 4 else 0
        except ValueError:
            page = 0
        origin = (
            callback_parts[5]
            if len(callback_parts) > 5 and callback_parts[5] in {"home", "today"}
            else "home"
        )
        shift_back = f"tg:nav:shift:{shift.id}:{page}:{origin}"
        if target == "view":
            _show_patchwork_shift(chat.chat_id, shift, bot, page=page, origin=origin)
        elif target == "info":
            bot.send_message(
                chat.chat_id,
                "Patchwork is the source of truth for work shifts. Any Telegram edit would be overwritten by the next Patchwork sync, so shifts are read-only here.",
                reply_markup=_navigation_keyboard(
                    shift_back, back_label="⬅️ Back to shift"
                ),
            )
        return
    if action == "ia" and len(parts) == 4:
        try:
            inbox_action = TelegramInboxAction.objects.get(
                pk=int(parts[3]), chat=chat, requested_by=user
            )
        except (ValueError, TelegramInboxAction.DoesNotExist):
            bot.send_message(
                chat.chat_id,
                "That change is no longer available.",
                reply_markup=_navigation_keyboard(),
            )
            return
        if target == "confirm":
            if inbox_action.proposed_data.get("cancel"):
                try:
                    event = CalendarEventRecord.objects.get(pk=inbox_action.object_id)
                    cancel_calendar_event(event)
                except (CalendarEventRecord.DoesNotExist, ValueError) as exc:
                    bot.send_message(chat.chat_id, html.escape(str(exc) or "That event is no longer available."), reply_markup=main_menu())
                else:
                    inbox_action.status = TelegramInboxAction.Status.COMPLETED
                    inbox_action.completed_at = timezone.now()
                    inbox_action.save(update_fields=["status", "completed_at", "updated_at"])
                    bot.send_message(chat.chat_id, "📅 Calendar event cancelled.", reply_markup=main_menu())
            else:
                _confirm_inbox_action(inbox_action, bot)
        elif target == "cancel" and inbox_action.status in [
            TelegramInboxAction.Status.AWAITING_INPUT, TelegramInboxAction.Status.AWAITING_CONFIRMATION
        ]:
            inbox_action.status = TelegramInboxAction.Status.CANCELLED
            inbox_action.completed_at = timezone.now()
            inbox_action.save(update_fields=["status", "completed_at", "updated_at"])
            bot.send_message(chat.chat_id, "Cancelled.", reply_markup=main_menu())
        return
    if action == "new":
        prompts = {
            "task": "Tell me the task, who it’s for, and optionally when it is due.",
            "event": "Tell me the event name, date, time, and any location.",
            "note": "What would you like me to remember?",
            "reminder": "What should I remind you about, when, and who should receive it?",
        }
        bot.send_message(
            chat.chat_id,
            prompts.get(target, "Tell me what you need."),
            reply_markup=_navigation_keyboard(),
        )
        return
    if action == "list":
        _list_tasks(chat.chat_id, bot) if target == "tasks" else _list_upcoming(chat.chat_id, bot)
        return
    if action == "done":
        task = Task.objects.filter(pk=target).exclude(
            status__in=[Task.Status.COMPLETED, Task.Status.CANCELLED]
        ).first()
        if not task:
            bot.send_message(chat.chat_id, "That task is no longer open.", reply_markup=main_menu())
            return
        complete_task(task)
        bot.send_message(chat.chat_id, f"☑️ Completed <b>{html.escape(task.title)}</b>.", reply_markup=main_menu())
        return

    try:
        conversation_id = int(target)
    except ValueError:
        return
    conversation = TelegramConversation.objects.select_related(
        "chat", "requested_by", "incoming_email", "parsed_action"
    ).filter(pk=conversation_id, chat=chat, requested_by=user).first()
    if not conversation:
        bot.send_message(
            chat.chat_id,
            "That request is no longer available.",
            reply_markup=_navigation_keyboard(),
        )
        return
    if action == "confirm":
        _confirm_conversation(conversation, bot)
    elif action == "cancel":
        if conversation.status in ACTIVE_STATUSES:
            _cancel_conversation(conversation, bot)
    elif action == "edit":
        if conversation.status != TelegramConversation.Status.AWAITING_CONFIRMATION:
            bot.send_message(
                chat.chat_id,
                "That request has already been handled.",
                reply_markup=_navigation_keyboard(),
            )
            return
        if conversation.parsed_action:
            conversation.parsed_action.delete()
            conversation.parsed_action = None
        prompt = "What should I change? Send the corrected detail or rewrite the request."
        conversation.status = TelegramConversation.Status.AWAITING_CLARIFICATION
        conversation.transcript = list(conversation.transcript) + [{"role": "assistant", "text": prompt}]
        conversation.save(update_fields=["parsed_action", "status", "transcript", "updated_at"])
        bot.send_message(chat.chat_id, prompt, reply_markup=_question_keyboard(conversation.id, []))
    elif action == "a" and len(parts) == 4:
        if conversation.status != TelegramConversation.Status.AWAITING_CLARIFICATION:
            return
        answer = parts[3]
        conversation.transcript = list(conversation.transcript) + [{"role": "user", "text": answer[:200]}]
        conversation.save(update_fields=["transcript", "updated_at"])
        _update_incoming_body(conversation)
        _extract_and_respond(conversation, bot)


def _claim_update(update_id: int) -> TelegramUpdate | None:
    """Claim an update briefly; never keep the transaction open over network I/O."""
    now = timezone.now()
    with transaction.atomic():
        TelegramUpdate.objects.get_or_create(update_id=update_id)
        update = TelegramUpdate.objects.select_for_update().get(update_id=update_id)
        if update.processed:
            return None
        if (
            update.processing_started_at
            and update.processing_started_at >= now - UPDATE_PROCESSING_LEASE
        ):
            return None
        update.processing_started_at = now
        update.error = ""
        update.save(update_fields=["processing_started_at", "error"])
        return update


def _finish_update(update: TelegramUpdate, *, update_type: str | None = None) -> None:
    if update_type is not None:
        update.update_type = update_type
    update.processed = True
    update.processing_started_at = None
    update.error = ""
    update.processed_at = timezone.now()
    update.save(
        update_fields=[
            "update_type", "processed", "processing_started_at", "error", "processed_at"
        ]
    )


def _release_update_claim(update_id: int) -> None:
    TelegramUpdate.objects.filter(update_id=update_id, processed=False).update(
        processing_started_at=None
    )


def _log_voice_failure(*, stage: str, update_id: int, exc: Exception) -> None:
    """Log only safe operational metadata, never voice or Telegram file data."""
    logger.warning(
        "Telegram voice handling failed stage=%s update_id=%s exception_type=%s",
        stage,
        update_id,
        type(exc).__name__,
    )


def _handle_voice_message(
    *, message: dict, chat: TelegramChat, user: TelegramUser, update_id: int, bot: TelegramBot
) -> None:
    voice = message.get("voice")
    if not isinstance(voice, dict):
        return

    duration = voice.get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
        bot.send_message(chat.chat_id, "I couldn’t read that voice note. Please try again or send text.")
        return
    if duration > MAX_VOICE_DURATION_SECONDS:
        bot.send_message(
            chat.chat_id,
            "That voice note is over 2 minutes. Please send a shorter note or send text.",
        )
        return

    advertised_size = voice.get("file_size")
    if advertised_size is not None and (
        not isinstance(advertised_size, int) or isinstance(advertised_size, bool)
        or advertised_size < 0
    ):
        bot.send_message(chat.chat_id, "I couldn’t read that voice note. Please try again or send text.")
        return
    if advertised_size is not None and advertised_size > MAX_VOICE_DOWNLOAD_BYTES:
        bot.send_message(
            chat.chat_id,
            "That voice note is over 5 MB. Please send a smaller note or send text.",
        )
        return

    file_id = voice.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        bot.send_message(chat.chat_id, "I couldn’t read that voice note. Please try again or send text.")
        return

    try:
        file_data = bot.get_file(file_id)
        file_path = file_data["file_path"]
    except (TelegramAPIError, KeyError, TypeError) as exc:
        _log_voice_failure(stage="get_file", update_id=update_id, exc=exc)
        bot.send_message(
            chat.chat_id,
            "I couldn’t transcribe that voice note clearly. Please try again or send text.",
        )
        return

    try:
        audio_bytes = bot.download_file(file_path, max_bytes=MAX_VOICE_DOWNLOAD_BYTES)
    except TelegramFileTooLargeError as exc:
        _log_voice_failure(stage="download", update_id=update_id, exc=exc)
        bot.send_message(
            chat.chat_id,
            "That voice note is over 5 MB. Please send a smaller note or send text.",
        )
        return
    except TelegramAPIError as exc:
        _log_voice_failure(stage="download", update_id=update_id, exc=exc)
        bot.send_message(
            chat.chat_id,
            "I couldn’t transcribe that voice note clearly. Please try again or send text.",
        )
        return

    try:
        transcript = transcribe_telegram_voice(
            audio_bytes,
            filename=bot.safe_audio_filename(file_path),
            content_type=str(voice.get("mime_type") or "audio/ogg"),
        )
    except (TelegramTranscriptionError, TypeError) as exc:
        _log_voice_failure(stage="transcription", update_id=update_id, exc=exc)
        bot.send_message(
            chat.chat_id,
            "I couldn’t transcribe that voice note clearly. Please try again or send text.",
        )
        return

    heard = html.escape(transcript[:3500])
    bot.send_message(chat.chat_id, f"🎙 I heard: “{heard}”")
    try:
        _handle_text(chat=chat, user=user, text=transcript, update_id=update_id, bot=bot)
    except Exception:
        # Preserve the webhook retry while keeping transcript-bearing SDK errors
        # out of logs and the TelegramUpdate error field.
        raise TelegramVoiceProcessingError("Voice command processing failed.") from None


def process_telegram_update(payload: dict, *, bot: TelegramBot | None = None) -> None:
    """Process one update idempotently. Raises on transient failures so
    Telegram retries the same update; already-completed update ids are no-ops."""
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        raise ValueError("Telegram update_id is missing or invalid.")

    update = _claim_update(update_id)
    if update is None:
        return
    try:
        callback = payload.get("callback_query")
        message = payload.get("message")
        envelope = callback.get("message") if callback else message
        sender_data = callback.get("from") if callback else (message or {}).get("from")
        if not envelope or not sender_data or not envelope.get("chat"):
            _finish_update(update, update_type="ignored")
            return

        user = _upsert_user(sender_data)
        if user is None:
            _finish_update(update, update_type="unauthorised")
            return
        chat = _upsert_chat(envelope["chat"], user)
        update.chat = chat
        update.user = user
        update.update_type = "callback_query" if callback else "message"
        update.save(update_fields=["chat", "user", "update_type"])

        bot = bot or TelegramBot()
        if callback:
            bot.answer_callback(str(callback.get("id", "")))
            if chat.authorised:
                _handle_callback(str(callback.get("data", "")), chat, user, bot)
        else:
            text = str(message.get("text", "")).strip()
            if text:
                command_match = re.match(r"^(/[^\s]+)", text)
                handled = command_match and _handle_command(
                    command_match.group(1), command_text=text, chat=chat, user=user, bot=bot
                )
                if not handled and chat.authorised:
                    _handle_text(chat=chat, user=user, text=text, update_id=update_id, bot=bot)
            elif message.get("voice") and chat.authorised:
                update.update_type = "voice_message"
                update.save(update_fields=["update_type"])
                _handle_voice_message(
                    message=message, chat=chat, user=user, update_id=update_id, bot=bot
                )

        _finish_update(update)
    except Exception:
        _release_update_claim(update_id)
        raise
