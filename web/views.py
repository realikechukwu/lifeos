"""Family web interface views (Phase 3).

No email-command business logic lives here. Task/Note creation uses plain
ModelForms; task completion/cancellation and ParsedAction approval/rejection
all delegate to the existing shared services in `assistant/services/*` —
the exact same functions Django admin actions use — so behaviour and audit
logging stay in one place.
"""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, connection
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from assistant.services.router import admin_approve_and_execute
from assistant.services.tasks import cancel_task, complete_task
from core.models import AssignedTo, AuditLog, Note, ParsedAction, Task

from .forms import NoteForm, ParsedActionReviewForm, TaskForm
from .services import build_calendar_feed, build_upcoming_feed, parse_date_param


def staff_required(view_func):
    """Like Django's @login_required, but for staff-only actions within the
    family app (as opposed to /admin/, which enforces this itself). Redirects
    with a message instead of a bare 403 so the UI stays usable."""

    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            messages.error(request, "You need staff access to do that.")
            return redirect("web:review_list")
        return view_func(request, *args, **kwargs)

    return _wrapped


# ---------------------------------------------------------------------------
# Progressive Web App metadata (public assets; never contains family data)
# ---------------------------------------------------------------------------

@require_GET
def web_manifest(request):
    """Serve an install manifest with storage-aware static asset URLs."""
    manifest = {
        "id": reverse("web:upcoming"),
        "name": "Family Assistant",
        "short_name": "Family",
        "description": "A private home for your family's plans, tasks, notes, and reminders.",
        "start_url": reverse("web:upcoming"),
        "scope": "/",
        "lang": "en-GB",
        "dir": "ltr",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "any",
        "background_color": "#f5f4fb",
        "theme_color": "#5847e6",
        "categories": ["productivity", "lifestyle"],
        "icons": [
            {
                "src": static("icons/app-icon.svg"),
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any",
            },
            {
                "src": static("icons/app-icon-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": static("icons/app-icon-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": static("icons/app-icon-maskable-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "maskable",
            },
            {
                "src": static("icons/app-icon-maskable-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
            {
                "src": static("icons/safari-pinned-tab.svg"),
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "monochrome",
            },
        ],
        "shortcuts": [
            {
                "name": "Upcoming",
                "short_name": "Upcoming",
                "description": "See what is coming up next",
                "url": reverse("web:upcoming"),
            },
            {
                "name": "Calendar",
                "short_name": "Calendar",
                "description": "Open the family calendar",
                "url": reverse("web:calendar"),
            },
            {
                "name": "New task",
                "short_name": "New task",
                "description": "Add a task for the family",
                "url": reverse("web:task_create"),
            },
            {
                "name": "Notes",
                "short_name": "Notes",
                "description": "Open family notes",
                "url": reverse("web:note_list"),
            },
        ],
        "launch_handler": {"client_mode": "navigate-existing"},
        "prefer_related_applications": False,
    }
    response = JsonResponse(manifest, json_dumps_params={"indent": 2})
    response["Content-Type"] = "application/manifest+json"
    response["Cache-Control"] = "public, max-age=3600"
    return response


@require_GET
def service_worker(request):
    """Serve the worker from / so its scope covers the whole application."""
    source = render_to_string("web/service-worker.js")
    response = HttpResponse(source, content_type="application/javascript; charset=utf-8")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Service-Worker-Allowed"] = "/"
    return response


# ---------------------------------------------------------------------------
# Health checks (unauthenticated — infrastructure endpoints, not app pages)
# ---------------------------------------------------------------------------

def healthz(request):
    """Original Phase 1 liveness check. Kept for backwards compatibility."""
    return JsonResponse({"status": "ok"})


def health(request):
    """Phase 3 health endpoint. Confirms Django is running and, optionally,
    that the database is reachable. Never calls Gmail, Google Calendar, or
    OpenAI — this must stay fast and dependency-free for load balancer /
    uptime checks."""
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        db_ok = False

    payload = {"status": "ok" if db_ok else "error", "database": "ok" if db_ok else "unreachable"}
    return JsonResponse(payload, status=200 if db_ok else 503)


# ---------------------------------------------------------------------------
# Upcoming (default landing page)
# ---------------------------------------------------------------------------

@login_required
def upcoming(request):
    items = build_upcoming_feed(request.user)
    return render(request, "web/upcoming.html", {"items": items})


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

@login_required
def calendar_page(request):
    return render(request, "web/calendar.html")


@login_required
def calendar_events_json(request):
    start = parse_date_param(request.GET.get("start"))
    end = parse_date_param(request.GET.get("end"))
    events = build_calendar_feed(start, end)
    return JsonResponse(events, safe=False)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@login_required
def task_list(request):
    tasks = Task.objects.all()
    status = request.GET.get("status", "")
    assigned_to = request.GET.get("assigned_to", "")
    q = request.GET.get("q", "").strip()
    overdue_only = request.GET.get("overdue") == "1"
    today = timezone.localdate()

    if status:
        tasks = tasks.filter(status=status)
    if assigned_to:
        tasks = tasks.filter(assigned_to=assigned_to)
    if q:
        tasks = tasks.filter(Q(title__icontains=q) | Q(description__icontains=q))
    if overdue_only:
        tasks = tasks.filter(
            due_date__lt=today, status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS]
        )

    highlight_raw = request.GET.get("highlight", "")
    highlight_id = int(highlight_raw) if highlight_raw.isdigit() else None

    return render(request, "web/task_list.html", {
        "tasks": tasks,
        "status_choices": Task.Status.choices,
        "assigned_to_choices": AssignedTo.choices,
        "current_status": status,
        "current_assigned_to": assigned_to,
        "current_q": q,
        "overdue_only": overdue_only,
        "today": today,
        "highlight_id": highlight_id,
    })


@login_required
def task_create(request):
    if request.method == "POST":
        form = TaskForm(request.POST)
        if form.is_valid():
            task = form.save()
            AuditLog.objects.create(
                action="task_created_via_web", object_type="Task", object_id=str(task.id),
                success=True, details={"title": task.title, "created_by": request.user.get_username()},
            )
            messages.success(request, f"Task “{task.title}” created.")
            return redirect("web:task_list")
        messages.error(request, "Please fix the errors below.")
    else:
        form = TaskForm()
    return render(request, "web/task_form.html", {"form": form, "mode": "create"})


@login_required
def task_edit(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if request.method == "POST":
        form = TaskForm(request.POST, instance=task)
        if form.is_valid():
            form.save()
            AuditLog.objects.create(
                action="task_edited_via_web", object_type="Task", object_id=str(task.id),
                success=True, details={"title": task.title, "edited_by": request.user.get_username()},
            )
            messages.success(request, f"Task “{task.title}” updated.")
            return redirect("web:task_list")
        messages.error(request, "Please fix the errors below.")
    else:
        form = TaskForm(instance=task)
    return render(request, "web/task_form.html", {"form": form, "mode": "edit", "task": task})


@login_required
@require_POST
def task_complete(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if task.status == Task.Status.COMPLETED:
        messages.info(request, f"“{task.title}” is already complete.")
    else:
        complete_task(task)
        messages.success(request, f"Marked “{task.title}” complete.")
    return redirect("web:task_list")


@login_required
@require_POST
def task_cancel(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if task.status in (Task.Status.CANCELLED, Task.Status.COMPLETED):
        messages.info(request, f"“{task.title}” is already {task.get_status_display().lower()}.")
    else:
        cancel_task(task)
        messages.success(request, f"Cancelled “{task.title}”.")
    return redirect("web:task_list")


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

@login_required
def note_list(request):
    notes = Note.objects.all()
    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")

    if q:
        notes = notes.filter(Q(title__icontains=q) | Q(body__icontains=q))
    if category:
        notes = notes.filter(category=category)

    categories = (
        Note.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")
    )

    return render(request, "web/note_list.html", {
        "notes": notes, "current_q": q, "current_category": category, "categories": categories,
    })


@login_required
def note_create(request):
    if request.method == "POST":
        form = NoteForm(request.POST)
        if form.is_valid():
            note = form.save()
            AuditLog.objects.create(
                action="note_created_via_web", object_type="Note", object_id=str(note.id),
                success=True, details={"title": note.title, "created_by": request.user.get_username()},
            )
            messages.success(request, "Note created.")
            return redirect("web:note_detail", pk=note.pk)
        messages.error(request, "Please fix the errors below.")
    else:
        form = NoteForm()
    return render(request, "web/note_form.html", {"form": form, "mode": "create"})


@login_required
def note_detail(request, pk):
    note = get_object_or_404(Note, pk=pk)
    return render(request, "web/note_detail.html", {"note": note})


@login_required
def note_edit(request, pk):
    note = get_object_or_404(Note, pk=pk)
    if request.method == "POST":
        form = NoteForm(request.POST, instance=note)
        if form.is_valid():
            form.save()
            AuditLog.objects.create(
                action="note_edited_via_web", object_type="Note", object_id=str(note.id),
                success=True, details={"title": note.title, "edited_by": request.user.get_username()},
            )
            messages.success(request, "Note updated.")
            return redirect("web:note_detail", pk=note.pk)
        messages.error(request, "Please fix the errors below.")
    else:
        form = NoteForm(instance=note)
    return render(request, "web/note_form.html", {"form": form, "mode": "edit", "note": note})


# ---------------------------------------------------------------------------
# Review (ParsedAction pending_review)
# ---------------------------------------------------------------------------

@login_required
def review_list(request):
    pending_qs = ParsedAction.objects.filter(status=ParsedAction.Status.PENDING_REVIEW)
    if not request.user.is_staff:
        return render(request, "web/review_list.html", {
            "is_staff": False, "pending_count": pending_qs.count(),
        })

    actions = pending_qs.select_related("incoming_email").order_by("-created_at")
    return render(request, "web/review_list.html", {
        "is_staff": True, "pending_count": actions.count(), "actions": actions,
    })


@staff_required
def review_detail(request, pk):
    action = get_object_or_404(ParsedAction, pk=pk)

    if request.method == "POST":
        intent = request.POST.get("intent")

        if intent == "reject":
            if action.status != ParsedAction.Status.EXECUTED:
                action.status = ParsedAction.Status.REJECTED
                action.reviewed_by = request.user
                action.reviewed_at = timezone.now()
                action.save(update_fields=["status", "reviewed_by", "reviewed_at"])
                AuditLog.objects.create(
                    action="parsed_action_rejected_via_web", object_type="ParsedAction", object_id=str(action.id),
                    success=True, details={"rejected_by": request.user.get_username()},
                )
                messages.success(request, "Action rejected.")
            return redirect("web:review_list")

        form = ParsedActionReviewForm(request.POST, instance=action)
        if not form.is_valid():
            messages.error(request, "Please fix the errors below.")
            return render(request, "web/review_detail.html", {"action": action, "form": form})
        form.save()

        if intent == "save":
            messages.success(request, "Changes saved.")
            return redirect("web:review_detail", pk=action.pk)

        if intent == "approve":
            action.reviewed_by = request.user
            action.reviewed_at = timezone.now()
            action.save(update_fields=["reviewed_by", "reviewed_at"])
            try:
                admin_approve_and_execute(action)
            except Exception as exc:  # noqa: BLE001 - show a safe message, never a traceback
                action.refresh_from_db()
                if action.status not in (ParsedAction.Status.REJECTED, ParsedAction.Status.EXECUTED):
                    action.status = ParsedAction.Status.FAILED
                    action.failure_reason = str(exc)[:2000]
                    action.save(update_fields=["status", "failure_reason"])
                messages.error(request, f"Approval failed: {exc}")
                return redirect("web:review_detail", pk=action.pk)

            action.refresh_from_db()
            if action.status == ParsedAction.Status.EXECUTED:
                messages.success(request, "Action approved and executed.")
                return redirect("web:review_list")
            if action.status == ParsedAction.Status.REJECTED:
                messages.warning(
                    request, action.failure_reason or "Not created — a duplicate was detected."
                )
                return redirect("web:review_list")
            messages.warning(request, "Still requires review — see the ambiguity notes below.")
            return redirect("web:review_detail", pk=action.pk)

    form = ParsedActionReviewForm(instance=action)
    return render(request, "web/review_detail.html", {"action": action, "form": form})
