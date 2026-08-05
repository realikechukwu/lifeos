"""Task creation, lookup, and completion. Single source of truth for both
the automatic email pipeline and the Django admin actions — no business
logic is duplicated between them.
"""

from django.conf import settings
from django.utils import timezone

from core.models import AssignedTo, AuditLog, HouseholdMember, ParsedAction, Task

from .extractor import is_plausible_date
from .gmail import is_authorised_sender


def normalise_title(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def passes_task_auto_create_gate(parsed_action: ParsedAction) -> tuple[bool, list[str]]:
    """Every condition required before a Task may be created automatically.
    Confidence alone never authorises creation."""
    reasons = []
    email = parsed_action.incoming_email

    if not is_authorised_sender(email.outer_sender):
        reasons.append("Sender is not authorised.")

    if parsed_action.action_type != ParsedAction.ActionType.CREATE_TASK:
        reasons.append("Action type is not create_task.")

    if not (parsed_action.title or "").strip():
        reasons.append("Title is missing.")

    if parsed_action.due_date is not None and not is_plausible_date(parsed_action.due_date):
        reasons.append("Due date is not plausible.")

    if parsed_action.assigned_to and parsed_action.assigned_to not in AssignedTo.values:
        reasons.append("Assignee is not one of the allowed values.")

    if parsed_action.confidence < settings.AUTOMATIC_ACTION_CONFIDENCE_THRESHOLD:
        reasons.append("Confidence is below the automatic action threshold.")

    if parsed_action.missing_fields:
        reasons.append("There are material missing fields.")

    if parsed_action.ambiguity_notes:
        reasons.append("There are ambiguity notes.")

    return (len(reasons) == 0, reasons)


def create_task_for_parsed_action(parsed_action: ParsedAction) -> Task:
    """Single execution path for turning a ParsedAction into a Task. Used by
    the automatic pipeline (after passes_task_auto_create_gate) and the
    admin 'Approve pending parsed actions' action (after human review)."""
    title = (parsed_action.title or "").strip()
    if not title:
        raise ValueError("Cannot create a task without a title.")

    assigned_to = parsed_action.assigned_to or AssignedTo.UNASSIGNED
    if assigned_to not in AssignedTo.values:
        raise ValueError("Cannot create a task with an unrecognised assignee.")

    created_by = HouseholdMember.objects.filter(
        email=parsed_action.incoming_email.outer_sender
    ).first()

    task = Task.objects.create(
        title=title,
        description=parsed_action.description or "",
        assigned_to=assigned_to,
        status=Task.Status.OPEN,
        priority=Task.Priority.NORMAL,
        due_date=parsed_action.due_date,
        due_time=parsed_action.due_time,
        source_email=parsed_action.incoming_email,
        source_parsed_action=parsed_action,
        created_by=created_by,
    )

    parsed_action.status = ParsedAction.Status.EXECUTED
    parsed_action.executed_at = timezone.now()
    parsed_action.save(update_fields=["status", "executed_at"])

    AuditLog.objects.create(
        source_email=parsed_action.incoming_email,
        action="create_task",
        object_type="Task",
        object_id=str(task.id),
        success=True,
        details={"title": task.title, "assigned_to": task.assigned_to, "due_date": str(task.due_date or "")},
    )
    return task


def find_matching_open_tasks(search_text: str) -> list[Task]:
    """Deterministic text matching only (no fuzzy-matching library): exact
    normalised title match first; otherwise a substring match either way."""
    search_text = (search_text or "").strip()
    if not search_text:
        return []
    normalised_search = normalise_title(search_text)

    open_tasks = list(Task.objects.exclude(status__in=[Task.Status.COMPLETED, Task.Status.CANCELLED]))

    exact_matches = [t for t in open_tasks if normalise_title(t.title) == normalised_search]
    if exact_matches:
        return exact_matches

    partial_matches = [
        t for t in open_tasks
        if normalised_search in normalise_title(t.title) or normalise_title(t.title) in normalised_search
    ]
    return partial_matches


def complete_task(task: Task, source_email=None) -> Task:
    """Single execution path for completing a task."""
    task.status = Task.Status.COMPLETED
    task.completed_at = timezone.now()
    task.save(update_fields=["status", "completed_at", "updated_at"])

    AuditLog.objects.create(
        source_email=source_email,
        action="task_completed",
        object_type="Task",
        object_id=str(task.id),
        success=True,
        details={"title": task.title},
    )
    return task
