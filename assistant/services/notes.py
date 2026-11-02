"""Note creation and edits. Single source of truth for the automatic email
pipeline, the Django admin approval action and the agent's note edits."""

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


MAX_NOTE_CHARS = 50000


class NoteEditError(ValueError):
    """An edit that cannot be applied exactly as written."""


def normalised_body(note: Note) -> str:
    """Browsers submit textareas with CRLF; edits match against plain newlines."""
    return note.body.replace("\r\n", "\n")


def apply_note_edits(body: str, *, edits=(), append: str | None = None, new_body: str | None = None) -> str:
    """Apply find/replace pairs in order, then append. Each find must match
    exactly once at the point it is applied, so text nobody quoted never changes."""
    if new_body is not None:
        result = new_body
    else:
        result = body
        for number, (find, replace) in enumerate(edits, 1):
            count = result.count(find)
            if count == 0:
                raise NoteEditError(f"Edit {number}: that text is not in the note. Read the note again and quote it exactly.")
            if count > 1:
                raise NoteEditError(
                    f"Edit {number}: that text appears {count} times in the note. Quote more of the line so it matches once."
                )
            start = result.index(find)
            end = start + len(find)
            if not replace and result[start - 1:start] in {"", "\n"} and result[end:end + 1] in {"", "\n"}:
                # Removing a whole line takes its line break with it.
                if end < len(result):
                    end += 1
                elif start > 0:
                    start -= 1
            result = result[:start] + replace + result[end:]
        if append:
            result = f"{result.rstrip()}\n{append}" if result.strip() else append
    if len(result) > MAX_NOTE_CHARS:
        raise NoteEditError(f"The note would be longer than {MAX_NOTE_CHARS} characters.")
    return result


def update_note(note: Note, *, title: str, body: str, role: str) -> Note:
    """Save an agent edit, keeping the previous version in the audit log so it can be restored."""
    previous = {"previous_title": note.title, "previous_body": note.body}
    note.title, note.body = title, body
    note.save(update_fields=["title", "body", "updated_at"])
    AuditLog.objects.create(
        action="update_note", object_type="Note", object_id=str(note.id),
        details={"role": role, **previous},
    )
    return note
