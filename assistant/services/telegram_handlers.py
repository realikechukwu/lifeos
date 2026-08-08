"""Telegram webhook orchestration and conversational interaction design."""

import html
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import (
    CalendarEventRecord,
    HouseholdMember,
    IncomingEmail,
    ParsedAction,
    Task,
    TelegramChat,
    TelegramConversation,
    TelegramUpdate,
    TelegramUser,
)

from .router import admin_approve_and_execute, build_parsed_action_from_extraction
from .tasks import complete_task
from .telegram import TelegramBot
from .telegram_extractor import extract_telegram_action

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (
    TelegramConversation.Status.AWAITING_CLARIFICATION,
    TelegramConversation.Status.AWAITING_CONFIRMATION,
)


def _button(text: str, callback_data: str) -> dict:
    return {"text": text, "callback_data": callback_data}


def _keyboard(*rows: list[dict]) -> dict:
    return {"inline_keyboard": list(rows)}


def main_menu() -> dict:
    return _keyboard(
        [_button("➕ Task", "tg:new:task"), _button("📅 Event", "tg:new:event")],
        [_button("📝 Note", "tg:new:note"), _button("⏰ Reminder", "tg:new:reminder")],
        [_button("✅ Open tasks", "tg:list:tasks"), _button("🗓 Upcoming", "tg:list:upcoming")],
    )


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
    if re.search(
        r"\b(show|list|what|anything)\b.*\b(upcoming|calendar|today|tomorrow|week|fortnight)\b",
        normalised,
    ):
        _list_upcoming(chat.chat_id, bot)
        return
    conversation = _new_conversation(chat, user, text, update_id)
    _extract_and_respond(conversation, bot)


def _list_tasks(chat_id: int, bot: TelegramBot) -> None:
    tasks = list(
        Task.objects.exclude(status__in=[Task.Status.COMPLETED, Task.Status.CANCELLED])
        .order_by("due_date", "created_at")[:10]
    )
    if not tasks:
        bot.send_message(chat_id, "No open tasks. Nicely done.", reply_markup=main_menu())
        return
    lines = ["✅ <b>Open tasks</b>"]
    rows = []
    for task in tasks:
        due = f" · due {task.due_date}" if task.due_date else ""
        lines.append(f"• {html.escape(task.title)}{due}")
        rows.append([_button(f"Done: {task.title[:28]}", f"tg:done:{task.id}")])
    rows.append([_button("Back", "tg:menu:main")])
    bot.send_message(chat_id, "\n".join(lines), reply_markup=_keyboard(*rows))


def _list_upcoming(chat_id: int, bot: TelegramBot) -> None:
    today = timezone.localdate()
    through = today + timedelta(days=14)
    events = CalendarEventRecord.objects.filter(
        appointment_date__gte=today, appointment_date__lte=through
    ).order_by("appointment_date", "start_time")[:10]
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
    bot.send_message(chat_id, "\n".join(lines), reply_markup=main_menu())


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
            bot.send_message(locked.chat.chat_id, "That request has already been handled.")
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
    command: str, *, chat: TelegramChat, user: TelegramUser, bot: TelegramBot
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
            "Hello — I’m LifeOS. Tell me naturally what you want to remember, schedule, or get done. I’ll ask if anything is unclear and show you a confirmation before acting.",
            reply_markup=main_menu(),
        )
        return True
    if command == "/tasks":
        _list_tasks(chat.chat_id, bot)
        return True
    if command in ("/upcoming", "/calendar"):
        _list_upcoming(chat.chat_id, bot)
        return True
    if command == "/cancel":
        _cancel_active(chat, user, bot)
        return True
    return False


def _handle_callback(data: str, chat: TelegramChat, user: TelegramUser, bot: TelegramBot) -> None:
    parts = data.split(":", 3)
    if len(parts) < 3 or parts[0] != "tg":
        return
    action = parts[1]
    target = parts[2]
    if action == "menu":
        bot.send_message(chat.chat_id, "What would you like to do?", reply_markup=main_menu())
        return
    if action == "new":
        prompts = {
            "task": "Tell me the task, who it’s for, and optionally when it is due.",
            "event": "Tell me the event name, date, time, and any location.",
            "note": "What would you like me to remember?",
            "reminder": "What should I remind you about, when, and who should receive it?",
        }
        bot.send_message(chat.chat_id, prompts.get(target, "Tell me what you need."))
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
    ).filter(pk=conversation_id, chat=chat).first()
    if not conversation:
        bot.send_message(chat.chat_id, "That request is no longer available.")
        return
    if action == "confirm":
        _confirm_conversation(conversation, bot)
    elif action == "cancel":
        if conversation.status in ACTIVE_STATUSES:
            _cancel_conversation(conversation, bot)
    elif action == "edit":
        if conversation.status != TelegramConversation.Status.AWAITING_CONFIRMATION:
            bot.send_message(chat.chat_id, "That request has already been handled.")
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


def process_telegram_update(payload: dict, *, bot: TelegramBot | None = None) -> None:
    """Process one update idempotently. Raises on transient failures so
    Telegram retries the same update; already-completed update ids are no-ops."""
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        raise ValueError("Telegram update_id is missing or invalid.")

    update, _ = TelegramUpdate.objects.get_or_create(update_id=update_id)
    if update.processed:
        return

    callback = payload.get("callback_query")
    message = payload.get("message")
    envelope = callback.get("message") if callback else message
    sender_data = callback.get("from") if callback else (message or {}).get("from")
    if not envelope or not sender_data or not envelope.get("chat"):
        update.update_type = "ignored"
        update.processed = True
        update.processed_at = timezone.now()
        update.save(update_fields=["update_type", "processed", "processed_at"])
        return

    user = _upsert_user(sender_data)
    if user is None:
        update.update_type = "unauthorised"
        update.processed = True
        update.processed_at = timezone.now()
        update.save(update_fields=["update_type", "processed", "processed_at"])
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
                command_match.group(1), chat=chat, user=user, bot=bot
            )
            if not handled and chat.authorised:
                _handle_text(chat=chat, user=user, text=text, update_id=update_id, bot=bot)

    update.processed = True
    update.error = ""
    update.processed_at = timezone.now()
    update.save(update_fields=["processed", "error", "processed_at"])
