"""Family web interface views (Phase 3).

No email-command business logic lives here. Task/Note creation uses plain
ModelForms; task completion/cancellation and ParsedAction approval/rejection
all delegate to the existing shared services in `assistant/services/*` —
the exact same functions Django admin actions use — so behaviour and audit
logging stay in one place.
"""

from functools import wraps
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, connection
from django.db.models import F, Q
from django.http import HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from assistant.services.router import admin_approve_and_execute
from assistant.services.tasks import cancel_task, complete_task
from core.models import AssignedTo, AuditLog, Note, ParsedAction, Task

from .forms import NoteForm, ParsedActionReviewForm, QuickTaskForm, TaskForm
from .services import build_calendar_feed, build_upcoming_feed, build_upcoming_sections, parse_date_param


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
        "name": "LifeOS",
        "short_name": "LifeOS",
        "description": "Your family's plans, tasks, notes and reminders in one private place.",
        "start_url": reverse("web:upcoming"),
        "scope": "/",
        "lang": "en-GB",
        "dir": "ltr",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "any",
        "background_color": "#f2f2f6",
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
                "name": "Today",
                "short_name": "Today",
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
    sections = build_upcoming_sections(items)
    return render(request, "web/upcoming.html", {
        "items": items,
        "attention_items": [item for item in items if item.is_attention],
        "upcoming_sections": sections,
        "today_section": next(section for section in sections if section.is_today),
        "later_sections": [section for section in sections if not section.is_today],
        "now": timezone.now(),
    })


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

@login_required
def calendar_page(request):
    return render(request, "web/calendar.html", {"calendar_timezone": timezone.get_current_timezone_name()})


@login_required
def calendar_events_json(request):
    start = parse_date_param(request.GET.get("start"))
    end = parse_date_param(request.GET.get("end"))
    events = build_calendar_feed(start, end)
    return JsonResponse(events, safe=False)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

TASK_VIEWS = [("todo", "To do"), ("overdue", "Overdue"), ("done", "Done"), ("all", "All")]
OPEN_STATUSES = [Task.Status.OPEN, Task.Status.IN_PROGRESS]


def _wants_fragment(request) -> bool:
    """app.js asks for just the changed piece of the page with this header."""
    return request.headers.get("X-Fragment") == "1"


def _with_toast(response, text: str, level: str = "success"):
    response["X-Toast"] = quote(text)
    response["X-Toast-Level"] = level
    return response


def _safe_next(request, fallback: str) -> str:
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, request.is_secure()):
        return target
    return fallback


def _task_filters(params) -> dict:
    """Read the task list's filters from a query dict. The older `overdue=1`
    and `status=` links keep working."""
    show = params.get("show", "")
    if params.get("overdue") == "1":
        show = "overdue"
    if show not in dict(TASK_VIEWS):
        show = "" if params.get("status") else "todo"
    return {
        "show": show,
        "status": params.get("status", ""),
        "assigned_to": params.get("assigned_to", ""),
        "q": params.get("q", "").strip(),
    }


def _task_queryset(filters: dict):
    tasks = Task.objects.all()
    show = filters["show"]
    if filters["status"]:
        tasks = tasks.filter(status=filters["status"])
    elif show in ("todo", "overdue"):
        tasks = tasks.filter(status__in=OPEN_STATUSES)
    elif show == "done":
        tasks = tasks.filter(status=Task.Status.COMPLETED)
    if show == "overdue":
        tasks = tasks.filter(due_date__lt=timezone.localdate(), status__in=OPEN_STATUSES)
    if filters["assigned_to"]:
        tasks = tasks.filter(assigned_to=filters["assigned_to"])
    if filters["q"]:
        tasks = tasks.filter(Q(title__icontains=filters["q"]) | Q(description__icontains=filters["q"]))
    if show in ("todo", "overdue"):
        return tasks.order_by(F("due_date").asc(nulls_last=True), "-created_at")
    if show == "done":
        return tasks.order_by(F("completed_at").desc(nulls_last=True))
    return tasks


def _task_list_context(request, params, *, next_url: str = "") -> dict:
    filters = _task_filters(params)
    highlight_raw = params.get("highlight", "")
    keep = {key: filters[key] for key in ("assigned_to", "q") if filters[key]}
    view_chips = [
        {"label": label, "query": urlencode({**keep, "show": value}), "active": filters["show"] == value}
        for value, label in TASK_VIEWS
    ]
    person_chips = []
    for value, label in AssignedTo.choices:
        if value == AssignedTo.UNASSIGNED:
            continue
        query = {"show": filters["show"] or "todo"}
        if filters["q"]:
            query["q"] = filters["q"]
        active = filters["assigned_to"] == value
        if not active:  # tapping the active person again clears it
            query["assigned_to"] = value
        person_chips.append({"label": label, "query": urlencode(query), "active": active})
    return {
        "tasks": _task_queryset(filters),
        "filters": filters,
        "view_chips": view_chips,
        "person_chips": person_chips,
        "list_query": params.urlencode(),
        "highlight_id": int(highlight_raw) if highlight_raw.isdigit() else None,
        "next_url": next_url or request.get_full_path(),
        "status_choices": Task.Status.choices,
        "assigned_to_choices": AssignedTo.choices,
        "today": timezone.localdate(),
    }


@login_required
def task_list(request):
    context = _task_list_context(request, request.GET)
    if _wants_fragment(request):
        return render(request, "web/_task_results.html", context)
    return render(request, "web/task_list.html", context)


@login_required
@require_POST
def task_quick_add(request):
    """The one-line add box on the Tasks page and in the New sheet."""
    form = QuickTaskForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the task a title.")
        return redirect(_safe_next(request, reverse("web:task_list")))
    task = form.save()
    AuditLog.objects.create(
        action="task_created_via_web", object_type="Task", object_id=str(task.id),
        success=True, details={"title": task.title, "created_by": request.user.get_username()},
    )
    if _wants_fragment(request):
        params = QueryDict(request.POST.get("list_query", ""))
        context = _task_list_context(request, params, next_url=_safe_next(request, reverse("web:task_list")))
        context["highlight_id"] = task.id
        return _with_toast(render(request, "web/_task_results.html", context), f"Added “{task.title}”")
    messages.success(request, f"Added “{task.title}”.")
    return redirect(_safe_next(request, reverse("web:task_list")))


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


def _task_toggle_done(request, task, toast: str, level: str):
    """Answer a tick-circle tap: the redrawn row for app.js, or a redirect."""
    if _wants_fragment(request):
        next_url = _safe_next(request, reverse("web:task_list"))
        response = render(request, "web/_task_row.html", {"task": task, "next_url": next_url})
        return _with_toast(response, toast, level)
    messages.add_message(request, messages.SUCCESS if level == "success" else messages.INFO, toast)
    return redirect(_safe_next(request, reverse("web:task_list")))


@login_required
@require_POST
def task_complete(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if task.status == Task.Status.COMPLETED:
        return _task_toggle_done(request, task, f"“{task.title}” is already complete.", "info")
    complete_task(task)
    return _task_toggle_done(request, task, f"Marked “{task.title}” done.", "success")


@login_required
@require_POST
def task_reopen(request, pk):
    """Untick a completed task so it is open again."""
    task = get_object_or_404(Task, pk=pk)
    if task.status in OPEN_STATUSES:
        return _task_toggle_done(request, task, f"“{task.title}” is already open.", "info")
    task.status = Task.Status.OPEN
    task.completed_at = None
    task.save(update_fields=["status", "completed_at", "updated_at"])
    AuditLog.objects.create(
        action="task_reopened_via_web", object_type="Task", object_id=str(task.id),
        success=True, details={"title": task.title, "reopened_by": request.user.get_username()},
    )
    return _task_toggle_done(request, task, f"Moved “{task.title}” back to To do.", "info")


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

    context = {
        "notes": notes, "current_q": q, "current_category": category, "categories": _note_categories(),
    }
    if _wants_fragment(request):
        return render(request, "web/_note_results.html", context)
    return render(request, "web/note_list.html", context)


def _note_categories():
    return Note.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")


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
    return render(request, "web/note_form.html", {"form": form, "mode": "create", "categories": _note_categories()})


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
    return render(request, "web/note_form.html", {
        "form": form, "mode": "edit", "note": note, "categories": _note_categories(),
    })


@login_required
@require_POST
def note_delete(request, pk):
    note = get_object_or_404(Note, pk=pk)
    note_id, label = note.id, str(note)
    # Keep the full text in the audit log so a mistaken delete can be recovered.
    AuditLog.objects.create(
        action="note_deleted_via_web", object_type="Note", object_id=str(note_id),
        success=True, details={
            "title": note.title, "body": note.body, "category": note.category,
            "deleted_by": request.user.get_username(),
        },
    )
    note.delete()
    messages.success(request, f"Deleted “{label}”.")
    return redirect("web:note_list")


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
