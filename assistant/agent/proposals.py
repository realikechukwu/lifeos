"""Validated, confirmable changes shared by agent channels.

A conditional status update claims a decision before any external side effect.
Uncertain executions are never retried automatically.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from functools import partial
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError

from core.models import (
    AgentOutcome, AgentProposal, AuditLog, CalendarEventRecord, IncomingEmail,
    ParsedAction, Reminder, Task,
)
from assistant.services.extractor import (
    AssistantAction, build_recurrence, combine_date_and_time, parse_24h_time, parse_iso_date,
)
from assistant.services import google_calendar, router, tasks, telegram_inbox
from assistant.services.reminders import evaluate_reminder_fields
from assistant.services.telegram import TelegramAPIError
from .formatting import to_telegram_html


class ToolInputError(ValueError):
    """Invalid tool input, safe to return to the model."""


CREATE_TYPES = {
    "create_event": "create_calendar_event", "create_task": "create_task",
    "create_note": "create_note", "create_reminder": "create_email_reminder",
}
COMMON_FIELDS = {"title", "description", "related_person", "notify_both"}
REMINDER_FIELDS = {"reminder_date", "reminder_time", "reminder_recipient"}
RECURRENCE_FIELDS = {
    "recurrence_frequency", "recurrence_interval", "recurrence_days_of_week",
    "recurrence_until", "recurrence_count",
}
CREATE_FIELDS = {
    "create_event": COMMON_FIELDS | REMINDER_FIELDS | RECURRENCE_FIELDS | {
        "appointment_date", "start_time", "end_time", "all_day", "location",
        "meeting_url", "organiser", "booking_reference", "reminder_lead_days",
    },
    "create_task": COMMON_FIELDS | {"due_date", "due_time", "assigned_to"},
    "create_note": COMMON_FIELDS | {"note_category"},
    "create_reminder": COMMON_FIELDS | REMINDER_FIELDS,
}
EDIT_FIELDS = {
    "complete_task": {"task_id"},
    "update_task": {"task_id", "title", "due_date", "assigned_to"},
    "reschedule_event": {"event_id", "appointment_date", "start_time", "all_day"},
    "cancel_event": {"event_id"}, "cancel_reminder": {"reminder_id"},
}


def _id(value, label):
    if type(value) is not int or value < 1:
        raise ToolInputError(f"{label} must be a positive integer from a list tool.")
    return value


def _date(value, label, *, required=False):
    parsed = parse_iso_date(value) if isinstance(value, str) else None
    if (value is not None or required) and (parsed is None or parsed.isoformat() != value):
        raise ToolInputError(f"{label} must be a date in YYYY-MM-DD form.")
    if parsed and parsed < timezone.localdate():
        raise ToolInputError(f"{label} cannot be in the past.")
    return parsed


def _time(value, label, *, required=False):
    parsed = parse_24h_time(value) if isinstance(value, str) else None
    if (value is not None or required) and parsed is None:
        raise ToolInputError(f"{label} must be a 24-hour time (HH:MM).")
    return parsed


def _future(day, clock):
    if clock is None:
        return
    when = combine_date_and_time(day, clock)
    if when <= timezone.now():
        raise ToolInputError("The requested time must be in the future.")
    # ZoneInfo accepts imaginary local times during the spring clock change.
    if timezone.localtime(when.astimezone(dt_timezone.utc)).replace(tzinfo=None) != datetime.combine(day, clock):
        raise ToolInputError("That local time does not exist because the clocks change.")


def _record(model, value, label):
    obj = model.objects.filter(pk=_id(value, label)).first()
    if obj is None:
        raise ToolInputError(f"No {label} with that id exists.")
    return obj


def _title(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ToolInputError("title must contain 1–255 characters.")
    return value.strip()


def validate_payload(kind, args):
    allowed = CREATE_FIELDS.get(kind, EDIT_FIELDS.get(kind, set()))
    if set(args) - allowed:
        raise ToolInputError("Unexpected fields: " + ", ".join(sorted(set(args) - allowed)))
    if kind in CREATE_TYPES:
        try:
            action = AssistantAction.model_validate(
                {**args, "action_type": CREATE_TYPES[kind], "confidence": 1.0}, strict=True,
            )
        except ValidationError as exc:
            raise ToolInputError(str(exc)) from None
        action.title = _title(action.title or ("Note" if kind == "create_note" else None))
        if action.description and len(action.description) > 20000:
            raise ToolInputError("description must be under 20000 characters.")
        if kind == "create_note" and not (action.description or args.get("title")):
            raise ToolInputError("A note needs a title or description.")
        if kind == "create_event":
            day = _date(action.appointment_date, "appointment_date", required=True)
            start = _time(action.start_time, "start_time", required=not action.all_day)
            end = _time(action.end_time, "end_time")
            if action.all_day and (start or end):
                raise ToolInputError("All-day events cannot have start/end times.")
            _future(day, start)
            if end and (not start or end <= start):
                raise ToolInputError("end_time must be after start_time on the same day.")
            if start and not end and start.hour == 23:
                raise ToolInputError("Give an end_time before midnight for a late event.")
            until = _date(action.recurrence_until, "recurrence_until")
            if not action.recurrence_frequency and any(
                args.get(f) is not None for f in RECURRENCE_FIELDS - {"recurrence_frequency"}
            ):
                raise ToolInputError("Recurrence details require recurrence_frequency.")
            if action.recurrence_days_of_week and action.recurrence_frequency != "weekly":
                raise ToolInputError("Weekdays are supported only for weekly recurrence.")
            recurrence = build_recurrence(
                action.recurrence_frequency, action.recurrence_interval,
                action.recurrence_days_of_week, until, action.recurrence_count, day, action.all_day,
            )
            if recurrence.note:
                raise ToolInputError(recurrence.note)
            wants_reminder = any(args.get(f) is not None for f in REMINDER_FIELDS | {"reminder_lead_days"})
            if wants_reminder:
                rd = _date(action.reminder_date, "reminder_date")
                rt = _time(action.reminder_time, "reminder_time")
                if action.reminder_lead_days is not None:
                    if action.reminder_lead_days < 0:
                        raise ToolInputError("reminder_lead_days cannot be negative.")
                    computed = day - timedelta(days=action.reminder_lead_days)
                    if rd and rd != computed:
                        raise ToolInputError("The reminder date conflicts with reminder_lead_days.")
                    rd = computed
                    rt = rt or start
                ok, reasons = evaluate_reminder_fields(rd, rt, action.reminder_recipient, 1, False)
                if not ok:
                    raise ToolInputError(" ".join(reasons))
        elif kind == "create_task":
            day = _date(action.due_date, "due_date")
            clock = _time(action.due_time, "due_time")
            if clock and not day:
                raise ToolInputError("due_time needs a due_date.")
            if day:
                _future(day, clock)
        elif kind == "create_reminder":
            day = _date(action.reminder_date, "reminder_date", required=True)
            clock = _time(action.reminder_time, "reminder_time", required=True)
            ok, reasons = evaluate_reminder_fields(day, clock, action.reminder_recipient, 1, False)
            if not ok:
                raise ToolInputError(" ".join(reasons))
        return action.model_dump(mode="json")

    data = dict(args)
    if kind in {"complete_task", "update_task"}:
        task = _record(Task, args.get("task_id"), "task_id")
        if task.status in {Task.Status.COMPLETED, Task.Status.CANCELLED}:
            raise ToolInputError("Only open tasks can be completed or edited.")
        data["title"] = task.title
        if kind == "update_task":
            if not set(args) & {"title", "due_date", "assigned_to"}:
                raise ToolInputError("Supply at least one task field to change.")
            data["title"] = _title(args.get("title", task.title))
            data["due_date"] = args.get("due_date", task.due_date.isoformat() if task.due_date else None)
            _date(data["due_date"], "due_date")
            data["assigned_to"] = args.get("assigned_to", task.assigned_to)
            if data["assigned_to"] not in {"ike", "wife", "both", "unassigned"}:
                raise ToolInputError("Choose ike, wife, both, or unassigned.")
    elif kind in {"reschedule_event", "cancel_event"}:
        event = _record(CalendarEventRecord, args.get("event_id"), "event_id")
        if event.recurrence_rule or not event.google_event_id or event.calendar_id != settings.GOOGLE_CALENDAR_ID:
            raise ToolInputError("Only LifeOS-owned one-off events on the shared calendar can be changed.")
        data["title"] = event.title
        if kind == "reschedule_event":
            if type(args.get("all_day", False)) is not bool:
                raise ToolInputError("all_day must be a boolean.")
            day = _date(args.get("appointment_date"), "appointment_date", required=True)
            start = _time(args.get("start_time"), "start_time", required=not args.get("all_day", False))
            if args.get("all_day") and start:
                raise ToolInputError("An all-day event cannot have a start time.")
            _future(day, start)
    elif kind == "cancel_reminder":
        reminder = _record(Reminder, args.get("reminder_id"), "reminder_id")
        if reminder.status not in {Reminder.Status.PENDING, Reminder.Status.FAILED}:
            raise ToolInputError("Only unsent reminders can be cancelled.")
        data["title"] = reminder.title
    else:
        raise ToolInputError("Unsupported proposal kind.")
    return data


def summary(proposal):
    p = proposal.payload
    lines = [f"Proposal {proposal.id}: {proposal.get_kind_display()}", p.get("title", "")]
    for key, label in (
        ("appointment_date", "Date"), ("start_time", "Start"), ("end_time", "End"),
        ("location", "Location"), ("meeting_url", "Meeting link"),
        ("assigned_to", "For"), ("due_date", "Due"), ("reminder_date", "Reminder date"),
        ("reminder_time", "Reminder time"), ("reminder_recipient", "Reminder for"),
        ("reminder_lead_days", "Reminder days before"), ("recurrence_frequency", "Repeats"),
        ("recurrence_interval", "Every"), ("recurrence_days_of_week", "Weekdays"),
        ("recurrence_until", "Until"), ("recurrence_count", "Occurrences"),
        ("description", "Details"),
    ):
        if p.get(key) is not None and p.get(key) != "":
            value = str(p[key])
            lines.append(f"{label}: {value[:1500]}" + ("… (preview)" if len(value) > 1500 else ""))
    if p.get("all_day"):
        lines.append("All day")
    if p.get("notify_both"):
        lines.append("Confirmation CC: the other household member")
    return "\n".join(lines)[:3300]


def record_outcome(proposal, detail):
    AgentOutcome.objects.create(thread=proposal.thread, text=f"{summary(proposal)}: {detail}")


def edit_card(proposal, bot):
    if not proposal.telegram_message_id or bot is None:
        return
    detail = proposal.result.get("detail", proposal.get_status_display())
    try:
        bot.edit_message(proposal.telegram_chat.chat_id, proposal.telegram_message_id,
                         to_telegram_html(f"{summary(proposal)}\n\n{detail}"))
    except TelegramAPIError:
        # A missing/old Telegram card must not undo a recorded decision.
        pass


def propose(ctx, args, *, kind):
    if not settings.AGENT_PROPOSALS_ENABLED:
        raise ToolInputError("Native proposals are disabled by the owner.")
    if ctx.thread is None:
        raise ToolInputError("Proposals require a conversation.")
    if ctx.channel == "email":
        if not settings.AGENT_EMAIL_ENABLED or not ctx.incoming_email or not ctx.newest_instruction.strip():
            raise ToolInputError("Email changes require the authorised sender's own new instruction.")
    elif ctx.telegram_user is None or ctx.telegram_chat is None:
        raise ToolInputError("Proposals require a Telegram conversation.")
    raw = dict(args)
    replaces_id = raw.pop("replaces_proposal_id", None)
    payload = validate_payload(kind, raw)
    old = None
    with transaction.atomic():
        if replaces_id is not None:
            old = AgentProposal.objects.filter(
                pk=_id(replaces_id, "replaces_proposal_id"), thread=ctx.thread,
                role=ctx.role, telegram_user=ctx.telegram_user, status="pending",
            ).first()
            if old is None or old.created_at <= timezone.now() - timedelta(hours=24):
                raise ToolInputError("Only your unexpired pending proposals in this thread can be replaced.")
            if not AgentProposal.objects.filter(pk=old.pk, status="pending").update(
                status="superseded", decided_at=timezone.now(), result={"detail": "Replaced by a corrected proposal."},
            ):
                raise ToolInputError("That proposal has already been decided.")
        proposal = AgentProposal.objects.create(
            thread=ctx.thread, role=ctx.role, telegram_chat=ctx.telegram_chat,
            telegram_user=ctx.telegram_user, kind=kind, payload=payload, replaces=old,
        )
        AuditLog.objects.create(action="agent_proposal_created", object_type="AgentProposal", object_id=str(proposal.id))
        if old:
            old.refresh_from_db()
            record_outcome(old, f"superseded by proposal {proposal.id}")
    if old:
        edit_card(old, ctx.bot)
    ctx.proposal_ids.append(proposal.id)
    if ctx.channel == "email" and kind in {*CREATE_TYPES, "complete_task"}:
        decide(proposal, confirm=True)
        proposal.refresh_from_db()
    return {"proposal_id": proposal.id, "status": proposal.status, "summary": summary(proposal), "result": proposal.result}


def _execute(proposal):
    kind, p = proposal.kind, proposal.payload
    # Revalidate mutable references and dates at decision time.
    allowed = CREATE_FIELDS.get(kind, EDIT_FIELDS.get(kind))
    validate_payload(kind, {k: v for k, v in p.items() if k in allowed})
    if kind in CREATE_TYPES:
        from assistant.services.telegram_handlers import _authorised_email_for_role
        incoming, _ = IncomingEmail.objects.get_or_create(
            gmail_message_id=f"agent-proposal:{proposal.id}", defaults={
                "source": IncomingEmail.Source.TELEGRAM,
                "outer_sender": _authorised_email_for_role(proposal.role),
                "subject": p.get("title", "Agent proposal"), "body_text": summary(proposal),
                "received_at": timezone.now(),
            },
        )
        parsed = router.build_parsed_action_from_extraction(incoming, AssistantAction(**p))
        outcome, extra = router.admin_approve_and_execute(parsed)
        parsed.refresh_from_db()
        if parsed.status == ParsedAction.Status.REJECTED and "Duplicate" in parsed.failure_reason:
            outcome = "duplicate"
        elif parsed.status != ParsedAction.Status.EXECUTED:
            return {"outcome": "failed", "detail": parsed.failure_reason or "Requires review; no new event created."}
        incoming.status = IncomingEmail.Status.PROCESSED
        incoming.save(update_fields=["status"])
        result = {"outcome": outcome, "detail": outcome.replace("_", " "), "parsed_action_id": parsed.id}
        result["objects"] = {key: obj.pk for key, obj in extra.items() if obj is not None}
        result["notes"] = parsed.ambiguity_notes
        return result
    if kind in {"complete_task", "update_task"}:
        task = Task.objects.get(pk=p["task_id"])
        if kind == "complete_task":
            tasks.complete_task(task)
        else:
            telegram_inbox.update_task(task, title=p["title"], due_date=parse_iso_date(p.get("due_date")), assigned_to=p["assigned_to"])
    elif kind == "reschedule_event":
        google_calendar.reschedule_calendar_event(
            CalendarEventRecord.objects.get(pk=p["event_id"]),
            appointment_date=parse_iso_date(p["appointment_date"]), start_time=parse_24h_time(p.get("start_time")),
        )
    elif kind == "cancel_event":
        google_calendar.cancel_calendar_event(CalendarEventRecord.objects.get(pk=p["event_id"]))
    elif kind == "cancel_reminder":
        telegram_inbox.cancel_reminder(Reminder.objects.get(pk=p["reminder_id"]))
    return {"outcome": "done", "detail": f"Done: {proposal.get_kind_display().lower()}."}


def decide(proposal, *, confirm, bot=None):
    now = timezone.now()
    status = "expired" if proposal.created_at <= now - timedelta(hours=24) else ("confirmed" if confirm else "cancelled")
    initial = {"outcome": status, "detail": "Execution started; do not retry automatically." if status == "confirmed" else status.capitalize()}
    if not AgentProposal.objects.filter(pk=proposal.pk, status="pending").update(
        status=status, decided_at=now, result=initial,
    ):
        return False
    proposal.refresh_from_db()
    if status == "confirmed":
        try:
            proposal.result = _execute(proposal)
        except Exception as exc:
            proposal.result = {"outcome": "failed", "detail": f"Failed: {str(exc)[:800]}"}
        if proposal.result["outcome"] == "failed":
            proposal.status = "failed"
        proposal.save(update_fields=["status", "result"])
    AuditLog.objects.create(
        action="agent_proposal_decided", object_type="AgentProposal", object_id=str(proposal.pk),
        success=proposal.status != "failed", details={"status": proposal.status, "result": proposal.result},
    )
    record_outcome(proposal, proposal.result["detail"])
    edit_card(proposal, bot)
    return True


def handle_callback(data, chat, user, bot):
    parts = data.split(":")
    if len(parts) != 3 or parts[2] not in {"ok", "no"} or not parts[1].isdigit():
        return
    proposal = AgentProposal.objects.filter(pk=int(parts[1]), telegram_chat=chat, telegram_user=user).first()
    if proposal is None:
        bot.send_message(chat.chat_id, "Only the person who requested this proposal can decide it.")
        return
    if parts[2] == "ok" and not settings.AGENT_PROPOSALS_ENABLED:
        bot.send_message(chat.chat_id, "Native proposals are currently disabled.")
        return
    decide(proposal, confirm=parts[2] == "ok", bot=bot)


def send_cards(thread, bot):
    for proposal in thread.proposals.filter(status="pending", telegram_message_id__isnull=True):
        markup = {"inline_keyboard": [[
            {"text": "✅ Confirm", "callback_data": f"ag:{proposal.pk}:ok"},
            {"text": "✖️ Cancel", "callback_data": f"ag:{proposal.pk}:no"},
        ]]}
        sent = bot.send_message(proposal.telegram_chat.chat_id, to_telegram_html(summary(proposal)), reply_markup=markup)
        proposal.telegram_message_id = sent.get("message_id")
        proposal.save(update_fields=["telegram_message_id"])


def context_outcomes(thread):
    outcomes = list(thread.outcomes.filter(delivered_at__isnull=True))
    pending = thread.proposals.filter(status="pending", created_at__gt=timezone.now() - timedelta(hours=24))
    lines = ["Since your last reply:"] + [o.text for o in outcomes] if outcomes else []
    lines += ["Pending proposal: " + summary(p) for p in pending]
    return "\n".join(lines), [o.id for o in outcomes]


def confirm_proposal(ctx, args):
    if ctx.channel != "email" or not settings.AGENT_EMAIL_ENABLED or not settings.AGENT_PROPOSALS_ENABLED:
        raise ToolInputError("confirm_proposal works only in the enabled email channel.")
    if set(args) != {"proposal_id"}:
        raise ToolInputError("Supply only proposal_id.")
    proposal_id = _id(args.get("proposal_id"), "proposal_id")
    # Inspect only the newest sender-authored words, never quoted text or model assertions.
    confirmation = re.fullmatch(r"\s*yes(?:\s+(\d+))?\s*[.!]?\s*", ctx.newest_instruction, re.IGNORECASE)
    if not confirmation:
        raise ToolInputError("The newest email must explicitly say YES or YES <proposal id>.")
    if confirmation.group(1) and int(confirmation.group(1)) != proposal_id:
        raise ToolInputError("That is not the proposal the sender confirmed.")
    proposal = AgentProposal.objects.filter(
        pk=proposal_id, thread=ctx.thread, role=ctx.role, telegram_user__isnull=True,
    ).first()
    if proposal is None or not proposal.confirmation_email_id or proposal.confirmation_email_id == ctx.incoming_email.pk:
        raise ToolInputError("Only a proposal offered in an earlier email in this thread can be confirmed.")
    if proposal.confirmation_email.created_at >= ctx.incoming_email.created_at:
        raise ToolInputError("The confirming email must be newer than the proposal email.")
    if not confirmation.group(1) and ctx.thread.proposals.filter(
        role=ctx.role, status="pending", confirmation_email__isnull=False,
        created_at__gt=timezone.now() - timedelta(hours=24),
    ).exclude(pk=proposal.pk).exists():
        raise ToolInputError("There are several pending proposals. Ask for YES followed by the proposal id.")
    decide(proposal, confirm=True)
    proposal.refresh_from_db()
    ctx.proposal_ids.append(proposal.pk)
    return {"proposal_id": proposal.id, "status": proposal.status, "result": proposal.result}


CONFIRM_DEFINITION = {
    "name": "confirm_proposal", "description": "Confirm a previously emailed proposal only when the newest sender-authored email says YES or YES <id>. Email only; Telegram uses buttons.",
    "input_schema": {"type": "object", "properties": {"proposal_id": {"type": "integer"}}, "required": ["proposal_id"], "additionalProperties": False},
}


def tool_definitions():
    schema = AssistantAction.model_json_schema()["properties"]
    definitions = []
    for kind, fields in {**CREATE_FIELDS, **EDIT_FIELDS}.items():
        properties = {key: schema[key] if key in schema else {"type": "integer"} for key in sorted(fields)}
        properties["replaces_proposal_id"] = {"type": "integer", "description": "Supersede a pending proposal when correcting it."}
        name = {"create_event": "event", "create_task": "task", "create_note": "note", "create_reminder": "reminder"}.get(kind, kind)
        definitions.append({
            "name": f"propose_{name}",
            "description": f"Propose {kind.replace('_', ' ')}. Python validates; Telegram requires Confirm. Use ids from list tools. Task edits support title, due_date and assigned_to; they clear due_time.",
            "input_schema": {"type": "object", "properties": properties, "additionalProperties": False},
        })
    return definitions


HANDLERS = {
    definition["name"]: partial(propose, kind=kind)
    for kind, definition in zip([*CREATE_FIELDS, *EDIT_FIELDS], tool_definitions())
}
