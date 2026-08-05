"""Note creation. Single source of truth for both the automatic email
pipeline and the Django admin approval action."""

from django.conf import settings
from django.utils import timezone

from core.models import AuditLog, HouseholdMember, Note, ParsedAction

from .gmail import is_authorised_sender

TITLE_MAX_LEN = 60


def generate_title_from_body(body: str, max_len: int = TITLE_MAX_LEN) -> str:
    """Deterministic, no-LLM-call title: first sentence (or first max_len
    characters) of the body, trimmed at a word boundary. Never invents
    content that wasn't in the body."""
    body = (body or "").strip()
    if not body:
        return ""
    first_line = body.splitlines()[0].strip()
    for terminator in (". ", "! ", "? "):
        idx = first_line.find(terminator)
        if 0 < idx <= max_len:
            return first_line[:idx].strip()
    if len(first_line) <= max_len:
        return first_line
    truncated = first_line[:max_len].rsplit(" ", 1)[0].strip()
    return (truncated or first_line[:max_len]) + "…"


def passes_note_auto_create_gate(parsed_action: ParsedAction) -> tuple[bool, list[str]]:
    reasons = []
    email = parsed_action.incoming_email

    if not is_authorised_sender(email.outer_sender):
        reasons.append("Sender is not authorised.")

    if parsed_action.action_type != ParsedAction.ActionType.CREATE_NOTE:
        reasons.append("Action type is not create_note.")

    has_body = bool((parsed_action.description or "").strip())
    has_title = bool((parsed_action.title or "").strip())
    if not has_body and not has_title:
        reasons.append("Note has neither a body nor a title.")

    if parsed_action.confidence < settings.AUTOMATIC_ACTION_CONFIDENCE_THRESHOLD:
        reasons.append("Confidence is below the automatic action threshold.")

    if parsed_action.missing_fields:
        reasons.append("There are material missing fields.")

    if parsed_action.ambiguity_notes:
        reasons.append("There are ambiguity notes.")

    return (len(reasons) == 0, reasons)


def create_note_for_parsed_action(parsed_action: ParsedAction) -> Note:
    body = (parsed_action.description or "").strip()
    title = (parsed_action.title or "").strip()
    if not body and not title:
        raise ValueError("Cannot create a note without a title or body.")
    if not title:
        title = generate_title_from_body(body)

    related_member = None
    if parsed_action.assigned_to in ("ike", "wife"):
        related_member = HouseholdMember.objects.filter(role=parsed_action.assigned_to, active=True).first()

    note = Note.objects.create(
        title=title,
        body=body,
        category=parsed_action.note_category or "",
        related_household_member=related_member,
        source_email=parsed_action.incoming_email,
        source_parsed_action=parsed_action,
    )

    parsed_action.status = ParsedAction.Status.EXECUTED
    parsed_action.executed_at = timezone.now()
    parsed_action.save(update_fields=["status", "executed_at"])

    AuditLog.objects.create(
        source_email=parsed_action.incoming_email,
        action="create_note",
        object_type="Note",
        object_id=str(note.id),
        success=True,
        details={"title": note.title},
    )
    return note
