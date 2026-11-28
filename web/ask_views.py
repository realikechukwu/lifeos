"""Web UI endpoints for the LifeOS agent ("Ask").

All endpoints require an authenticated user, the AGENT_WEB_CHAT_ENABLED flag,
and (except the landing page, which renders a friendly setup notice) a linked
WebAccount. Write actions require POST and delegate directly to the shared
agent channel and proposal services.
"""

from datetime import date
import json
import re
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from assistant.agent.documents import DOCX_MIME
from assistant.agent.loop import REFUSAL_REPLY
from assistant.agent.proposals import ToolInputError, decide, summary as proposal_summary
from assistant.agent.web import close_thread, enqueue_web_message, greeting_name, is_thread_stale, open_thread_for
from core.models import AgentDocumentDelivery, AgentJob, AgentMessage, AgentProposal, AgentThread, GeneratedDocument, WebAccount

SUGGESTION_CHIPS = [
    "What's on this week?",
    "Add milk to the shopping list",
    "Anything in my email?",
]

STARTER_PROMPTS = [
    "What's on today?",
    "What's on this week?",
    "Add milk to the shopping list",
    "Anything in my email?",
]

PROPOSAL_LABELS = {
    "create_event": "New event",
    "create_task": "New task",
    "create_note": "New note",
    "create_reminder": "Reminder",
    "complete_task": "Complete task",
    "update_task": "Update task",
    "reschedule_event": "Reschedule event",
    "cancel_event": "Cancel event",
    "cancel_reminder": "Cancel reminder",
    "update_note": "Update note",
}

PROPOSAL_ICONS = {
    "create_event": ("calendar", ""),
    "reschedule_event": ("calendar", ""),
    "cancel_event": ("calendar", ""),
    "create_task": ("tasks", "warn"),
    "complete_task": ("tasks", "warn"),
    "update_task": ("tasks", "warn"),
    "create_note": ("note", "teal"),
    "update_note": ("note", "teal"),
    "create_reminder": ("bell", "ok"),
    "cancel_reminder": ("bell", "ok"),
}

PROPOSAL_CONFIRMED_TEXT = {
    "create_event": "Added to the family calendar",
    "create_task": "Added to Tasks",
    "create_note": "Saved to Notes",
    "create_reminder": "Reminder scheduled",
    "complete_task": "Marked done",
    "update_task": "Task updated",
    "reschedule_event": "Event rescheduled",
    "cancel_event": "Event cancelled",
    "cancel_reminder": "Reminder cancelled",
    "update_note": "Note updated",
}

PROPOSAL_CONFIRM_LABELS = {
    "create_event": "Add",
    "create_task": "Add",
    "create_note": "Add",
    "create_reminder": "Add",
    "complete_task": "Mark done",
    "update_task": "Save",
    "reschedule_event": "Save",
    "cancel_event": "Confirm",
    "cancel_reminder": "Confirm",
    "update_note": "Save",
}

_DATE_PREFIXES = ("Date: ", "Start: ", "End: ", "Due: ", "Reminder date: ", "Reminder time: ", "All day")


def _require_web_chat_enabled():
    if not settings.AGENT_WEB_CHAT_ENABLED:
        raise Http404


def _web_account_for(user) -> WebAccount | None:
    return WebAccount.objects.filter(user=user).select_related("user").first()


def _wants_fragment(request) -> bool:
    return request.headers.get("X-Fragment") == "1"


def _with_toast(response, text: str, level: str = "success"):
    response["X-Toast"] = quote(text)
    response["X-Toast-Level"] = level
    return response


def _greeting_for(account: WebAccount) -> str:
    hour = timezone.localtime().hour
    period = "Morning" if hour < 12 else ("Afternoon" if hour < 17 else "Evening")
    name = greeting_name(account)
    return f"{period}, {name}" if name else period


def _message_blocks(message: AgentMessage) -> list:
    if message.content_json:
        try:
            loaded = json.loads(message.content_json)
            if isinstance(loaded, list):
                return loaded
        except ValueError:
            pass
    return message.content if isinstance(message.content, list) else []


def _extract_user_display(blocks: list) -> tuple[str, str]:
    """Return (visible text, attachment label) for a stored user message, or ('', '') for tool results."""
    if not blocks or all(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
        return "", ""
    first_text = ""
    attachment = ""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            raw = str(block.get("text") or "").strip()
            if raw.startswith("Untrusted uploaded document: "):
                attachment = raw.removeprefix("Untrusted uploaded document: ").strip()
            elif raw == "Untrusted photo sent by the speaker:":
                attachment = attachment or "Photo"
            elif raw.startswith("Untrusted ") and "extracted text:" in raw:
                continue
            elif not first_text:
                first_text = raw
        elif btype == "image" and not attachment:
            attachment = "Photo"
        elif btype == "document" and not attachment:
            attachment = "PDF document"
    return first_text, attachment


def _extract_assistant_display(message: AgentMessage, blocks: list) -> str:
    """Extract the user-visible assistant reply text from a stored message."""
    if message.stop_reason == "tool_use":
        return ""
    parts = [
        str(b.get("text") or "").strip()
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text" and str(b.get("text") or "").strip()
    ]
    text = "\n\n".join(parts)
    if message.stop_reason == "refusal" and not text:
        return REFUSAL_REPLY
    if message.stop_reason == "max_tokens":
        text = (text + "\n\n(I ran out of room there; ask me to continue.)").strip()
    return text


def _extract_tool_result_ids(blocks: list) -> tuple[list[int], list[int]]:
    """Pull proposal_id and document_id references from tool_result blocks."""
    proposal_ids: list[int] = []
    document_ids: list[int] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_result" or block.get("is_error"):
            continue
        raw = block.get("content")
        if not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict):
            pid = payload.get("proposal_id")
            if type(pid) is int:
                proposal_ids.append(pid)
            did = payload.get("document_id")
            if type(did) is int and payload.get("status"):
                document_ids.append(did)
    return proposal_ids, document_ids


def _format_iso_date(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        parsed = date.fromisoformat(str(raw))
    except ValueError:
        return str(raw)
    return parsed.strftime("%a %-d %b")


def _when_line(proposal: AgentProposal) -> str:
    p = proposal.payload or {}
    if p.get("appointment_date"):
        day = _format_iso_date(p["appointment_date"])
        if p.get("all_day"):
            return f"{day} · All day"
        start = p.get("start_time")
        end = p.get("end_time")
        if start and end:
            return f"{day} · {start}–{end}"
        if start:
            return f"{day} · {start}"
        return day
    if p.get("reminder_date"):
        day = _format_iso_date(p["reminder_date"])
        clock = p.get("reminder_time")
        return f"{day} · {clock}" if clock else day
    if p.get("due_date"):
        day = _format_iso_date(p["due_date"])
        clock = p.get("due_time")
        return f"Due {day} · {clock}" if clock else f"Due {day}"
    return ""


def _proposal_status_text(proposal: AgentProposal) -> str:
    if proposal.status == AgentProposal.Status.CONFIRMED:
        if (proposal.result or {}).get("outcome") == "duplicate":
            return "Already in LifeOS"
        return PROPOSAL_CONFIRMED_TEXT.get(proposal.kind) or (proposal.result or {}).get("detail") or "Done"
    if proposal.status == AgentProposal.Status.CANCELLED:
        return "Cancelled"
    if proposal.status == AgentProposal.Status.SUPERSEDED:
        return "Replaced by a newer version"
    if proposal.status == AgentProposal.Status.EXPIRED:
        return "Expired"
    if proposal.status == AgentProposal.Status.FAILED:
        return (proposal.result or {}).get("detail") or "Could not complete this change"
    return ""


def present_proposal(proposal: AgentProposal) -> dict:
    """Build display context for one AgentProposal card using proposals.summary."""
    full_summary = proposal_summary(proposal)
    lines = [line for line in full_summary.splitlines() if line.strip()]
    title = lines[1] if len(lines) > 1 else (proposal.payload.get("title") or proposal.get_kind_display())
    when = _when_line(proposal)
    extra_lines = []
    for line in lines[2:]:
        if when and any(line == prefix or line.startswith(prefix) for prefix in _DATE_PREFIXES):
            continue
        extra_lines.append(line)
    icon_name, icon_tint = PROPOSAL_ICONS.get(proposal.kind, ("home", ""))
    confirm_label = PROPOSAL_CONFIRM_LABELS.get(
        proposal.kind,
        "Add" if proposal.kind.startswith("homebase_create_") else "Save",
    )
    return {
        "id": proposal.pk,
        "proposal": proposal,
        "status": proposal.status,
        "is_pending": proposal.status == AgentProposal.Status.PENDING,
        "is_confirmed": proposal.status == AgentProposal.Status.CONFIRMED,
        "is_failed": proposal.status == AgentProposal.Status.FAILED,
        "label": PROPOSAL_LABELS.get(proposal.kind, proposal.get_kind_display()),
        "title": title,
        "when": when,
        "extra_lines": extra_lines,
        "icon": icon_name,
        "icon_tint": icon_tint,
        "confirm_label": confirm_label,
        "status_text": _proposal_status_text(proposal),
        "change_prompt": f"Change {title}: ",
        "confirm_url": reverse("web:ask_proposal_confirm", args=[proposal.pk]),
        "cancel_url": reverse("web:ask_proposal_cancel", args=[proposal.pk]),
    }


def _build_timeline(thread: AgentThread | None) -> tuple[list[dict], int]:
    """Turn a thread's append-only AgentMessages into display items for initial Ask render."""
    if thread is None:
        return [], 0

    all_messages = list(thread.messages.order_by("id"))
    last_message_id = max((m.id for m in all_messages), default=0)
    proposals_by_id = {p.pk: p for p in thread.proposals.order_by("id")}
    deliveries = list(
        AgentDocumentDelivery.objects.filter(job__thread=thread)
        .select_related("document", "job")
        .defer("document__data", "document__content", "job__input_blocks", "job__reply_text")
        .order_by("id")
    )
    docs_by_id = {d.document_id: d.document for d in deliveries}

    items: list[dict] = []
    turn_proposal_ids: list[int] = []
    turn_document_ids: list[int] = []
    placed_proposals: set[int] = set()
    placed_docs: set[int] = set()

    for message in all_messages:
        blocks = _message_blocks(message)
        if message.role == AgentMessage.Role.USER:
            p_ids, d_ids = _extract_tool_result_ids(blocks)
            if p_ids or d_ids:
                turn_proposal_ids.extend(p_ids)
                turn_document_ids.extend(d_ids)
                continue
            user_text, attachment = _extract_user_display(blocks)
            if user_text or attachment:
                turn_proposal_ids.clear()
                turn_document_ids.clear()
                items.append({
                    "kind": "user",
                    "id": message.id,
                    "text": user_text,
                    "attachment": attachment,
                })
        elif message.role == AgentMessage.Role.ASSISTANT:
            reply_text = _extract_assistant_display(message, blocks)
            if not reply_text and not turn_proposal_ids and not turn_document_ids:
                continue
            cards = []
            for pid in turn_proposal_ids:
                if pid in proposals_by_id and pid not in placed_proposals:
                    cards.append(present_proposal(proposals_by_id[pid]))
                    placed_proposals.add(pid)
            docs = []
            for did in turn_document_ids:
                if did in docs_by_id and did not in placed_docs:
                    docs.append(docs_by_id[did])
                    placed_docs.add(did)
            turn_proposal_ids.clear()
            turn_document_ids.clear()
            pending_ids = [c["id"] for c in cards if c["is_pending"]]
            items.append({
                "kind": "assistant",
                "id": message.id,
                "text": reply_text,
                "cards": cards,
                "pending_ids": pending_ids,
                "group_id": f"proposals-msg-{message.id}",
                "documents": docs,
            })

    # Attach any remaining proposals or deliveries on this thread (for instance
    # when created directly or when a job finished without tool_result JSON).
    leftover_cards = [
        present_proposal(p) for pid, p in proposals_by_id.items() if pid not in placed_proposals
    ]
    leftover_docs = [doc for did, doc in docs_by_id.items() if did not in placed_docs]
    if leftover_cards or leftover_docs:
        if items and items[-1]["kind"] == "assistant":
            items[-1]["cards"].extend(leftover_cards)
            items[-1]["pending_ids"] = [c["id"] for c in items[-1]["cards"] if c["is_pending"]]
            items[-1]["documents"].extend(leftover_docs)
        else:
            pending_ids = [c["id"] for c in leftover_cards if c["is_pending"]]
            items.append({
                "kind": "assistant",
                "id": last_message_id,
                "text": "",
                "cards": leftover_cards,
                "pending_ids": pending_ids,
                "group_id": f"proposals-tail-{thread.id}",
                "documents": leftover_docs,
            })

    return items, last_message_id


def _build_updates(thread: AgentThread | None, *, after_id: int, job_active: bool = False) -> tuple[list[dict], int]:
    """Incremental poll builder: query only messages with id > after_id and
    resolve proposal cards and documents from finished jobs' reply_metadata."""
    if thread is None:
        return [], after_id

    # Lightweight scan of new message ids only (defers content/content_json).
    new_rows = list(
        thread.messages.filter(id__gt=after_id)
        .values_list("id", "role", "stop_reason")
        .order_by("id")
    )
    if not new_rows:
        return [], after_id

    last_message_id = new_rows[-1][0]
    reply_ids = [
        msg_id
        for msg_id, role, stop_reason in new_rows
        if role == AgentMessage.Role.ASSISTANT and stop_reason != "tool_use"
    ]
    if not reply_ids:
        return [], last_message_id

    reply_id_set = set(reply_ids)
    recorded_reply_ids: set[int] = set()
    proposals_for_msg: dict[int, list[int]] = {}
    docs_for_msg: dict[int, list[int]] = {}
    needed_proposal_ids: set[int] = set()
    needed_doc_ids: set[int] = set()
    for meta in (
        AgentJob.objects.filter(
            thread=thread,
            channel=AgentThread.Channel.WEB,
            status=AgentJob.Status.DONE,
        )
        .order_by("id")
        .values_list("reply_metadata", flat=True)
    ):
        if not isinstance(meta, dict):
            continue
        msg_id = meta.get("assistant_message_id")
        if msg_id not in reply_id_set:
            continue
        recorded_reply_ids.add(msg_id)
        p_ids = [pid for pid in (meta.get("proposal_ids") or []) if type(pid) is int]
        d_ids = [did for did in (meta.get("document_ids") or []) if type(did) is int]
        if p_ids:
            proposals_for_msg.setdefault(msg_id, []).extend(p_ids)
            needed_proposal_ids.update(p_ids)
        if d_ids:
            docs_for_msg.setdefault(msg_id, []).extend(d_ids)
            needed_doc_ids.update(d_ids)

    if job_active:
        # The worker saves the final reply a few queries before it records the
        # turn's proposal and document ids and marks the job done. Stop before
        # the first reply no finished job has recorded yet, so a later poll
        # returns it together with its cards instead of skipping past them.
        unrecorded = [msg_id for msg_id in reply_ids if msg_id not in recorded_reply_ids]
        if unrecorded:
            cutoff = unrecorded[0]
            reply_ids = [msg_id for msg_id in reply_ids if msg_id < cutoff]
            last_message_id = max((row[0] for row in new_rows if row[0] < cutoff), default=after_id)
            if not reply_ids:
                return [], last_message_id

    assistant_messages = list(
        thread.messages.filter(id__in=reply_ids)
        .only("id", "thread_id", "role", "stop_reason", "content", "content_json")
        .order_by("id")
    )

    proposals_by_id = (
        {p.pk: p for p in AgentProposal.objects.filter(pk__in=needed_proposal_ids, thread=thread)}
        if needed_proposal_ids
        else {}
    )
    docs_by_id = (
        {
            d.pk: d
            for d in GeneratedDocument.objects.filter(pk__in=needed_doc_ids).only("id", "title", "filename")
        }
        if needed_doc_ids
        else {}
    )

    items: list[dict] = []
    for message in assistant_messages:
        blocks = _message_blocks(message)
        reply_text = _extract_assistant_display(message, blocks)
        cards = [
            present_proposal(proposals_by_id[pid])
            for pid in dict.fromkeys(proposals_for_msg.get(message.id, []))
            if pid in proposals_by_id
        ]
        docs = [
            docs_by_id[did]
            for did in dict.fromkeys(docs_for_msg.get(message.id, []))
            if did in docs_by_id
        ]
        if not reply_text and not cards and not docs:
            continue
        pending_ids = [c["id"] for c in cards if c["is_pending"]]
        items.append({
            "kind": "assistant",
            "id": message.id,
            "text": reply_text,
            "cards": cards,
            "pending_ids": pending_ids,
            "group_id": f"proposals-msg-{message.id}",
            "documents": docs,
        })

    return items, last_message_id


def _active_web_job(user, *, include_text: bool = False) -> AgentJob | None:
    qs = AgentJob.objects.filter(
        channel=AgentThread.Channel.WEB,
        web_user=user,
        status__in=[AgentJob.Status.QUEUED, AgentJob.Status.RUNNING],
    ).select_related("thread")
    if include_text:
        qs = qs.defer("input_blocks", "reply_text")
    else:
        qs = qs.defer("input_blocks", "reply_text", "text")
    return qs.order_by("-id").first()


def _latest_failed_job(user, thread: AgentThread | None) -> AgentJob | None:
    """Return the latest web job if it failed on the current conversation."""
    latest = (
        AgentJob.objects.filter(channel=AgentThread.Channel.WEB, web_user=user)
        .only("id", "status", "thread_id", "created_at", "finished_at")
        .order_by("-id")
        .first()
    )
    if latest is None or latest.status != AgentJob.Status.FAILED:
        return None
    if thread is not None:
        if latest.thread_id not in (thread.id, None):
            return None
        last_reply_at = (
            thread.messages.filter(role=AgentMessage.Role.ASSISTANT)
            .order_by("-id")
            .values_list("created_at", flat=True)
            .first()
        )
        if last_reply_at and latest.finished_at and last_reply_at > latest.finished_at:
            return None
        return latest
    last_closed = (
        AgentThread.objects.filter(channel=AgentThread.Channel.WEB, web_user=user, closed_at__isnull=False)
        .order_by("-closed_at")
        .values_list("closed_at", flat=True)
        .first()
    )
    if latest.thread_id is not None:
        return None
    if last_closed and latest.created_at <= last_closed:
        return None
    return latest


@login_required
@require_GET
def ask_page(request):
    _require_web_chat_enabled()
    account = _web_account_for(request.user)
    if account is None:
        return render(request, "web/ask_not_setup.html")

    active_job = _active_web_job(request.user, include_text=True)
    if active_job and active_job.thread_id:
        thread = active_job.thread
    else:
        thread = open_thread_for(request.user)

    items, last_message_id = _build_timeline(thread)
    if active_job and active_job.thread_id is None and (active_job.text or active_job.reply_metadata.get("attachment_name")):
        items.append({
            "kind": "user",
            "id": None,
            "text": active_job.text,
            "attachment": active_job.reply_metadata.get("attachment_name", ""),
        })
    failed_job = None if active_job else _latest_failed_job(request.user, thread)

    return render(request, "web/ask.html", {
        "account": account,
        "thread": thread,
        "items": items,
        "last_message_id": last_message_id,
        "polling": active_job is not None,
        "failed_job": failed_job,
        "greeting": _greeting_for(account),
        "suggestion_chips": SUGGESTION_CHIPS,
        "starter_prompts": STARTER_PROMPTS,
    })


@login_required
@require_POST
def ask_send(request):
    _require_web_chat_enabled()
    account = _web_account_for(request.user)
    if account is None:
        return HttpResponse("Ask isn't set up for your account yet.", status=403)

    text = request.POST.get("message", "")
    uploaded_file = request.FILES.get("attachment")
    try:
        job = enqueue_web_message(user=request.user, account=account, text=text, uploaded_file=uploaded_file)
    except ToolInputError as exc:
        if _wants_fragment(request):
            response = HttpResponse(str(exc), status=400, content_type="text/plain; charset=utf-8")
            return _with_toast(response, str(exc), "error")
        messages.error(request, str(exc))
        return redirect("web:ask")

    if _wants_fragment(request):
        return render(request, "web/_ask_sent_fragment.html", {
            "user_text": job.text,
            "attachment": job.reply_metadata.get("attachment_name", ""),
        })
    return redirect("web:ask")


@login_required
@require_GET
def ask_updates(request):
    _require_web_chat_enabled()
    account = _web_account_for(request.user)
    if account is None:
        return JsonResponse({"error": "Ask isn't set up for your account yet."}, status=403)

    raw_after = request.GET.get("after", "0")
    after_id = int(raw_after) if raw_after.isdigit() else 0

    active_job = _active_web_job(request.user)
    if active_job and active_job.thread_id:
        thread = active_job.thread
    else:
        thread = (
            AgentThread.objects.filter(
                channel=AgentThread.Channel.WEB,
                web_user=request.user,
                closed_at__isnull=True,
            )
            .order_by("-last_activity_at")
            .first()
        )

    items, last_message_id = _build_updates(thread, after_id=after_id, job_active=active_job is not None)
    failed_job = None if (active_job or items) else _latest_failed_job(request.user, thread)
    html = ""
    if items:
        html = render_to_string("web/_ask_reply_items.html", {"items": items}, request=request)
    elif failed_job is not None:
        html = render_to_string("web/_ask_error.html", {"failed_job": failed_job}, request=request)

    return JsonResponse({
        "html": html,
        "last_message_id": last_message_id,
        "polling": active_job is not None,
        "failed": failed_job is not None,
    })


@login_required
@require_POST
def ask_new(request):
    _require_web_chat_enabled()
    account = _web_account_for(request.user)
    if account is None:
        return HttpResponse("Ask isn't set up for your account yet.", status=403)

    close_thread(request.user)
    if _wants_fragment(request):
        response = render(request, "web/_ask_empty.html", {
            "greeting": _greeting_for(account),
            "starter_prompts": STARTER_PROMPTS,
        })
        return _with_toast(response, "Started a new chat.", "info")
    return redirect("web:ask")


@login_required
@require_POST
def ask_retry(request, pk):
    _require_web_chat_enabled()
    account = _web_account_for(request.user)
    if account is None:
        return HttpResponse("Ask isn't set up for your account yet.", status=403)
    if not settings.AGENT_ENABLED:
        response = HttpResponse("The LifeOS assistant is currently switched off.", status=400, content_type="text/plain; charset=utf-8")
        return _with_toast(response, "The LifeOS assistant is currently switched off.", "error")

    job = get_object_or_404(
        AgentJob.objects.select_related("thread").defer("input_blocks", "reply_text", "text"),
        pk=pk,
        channel=AgentThread.Channel.WEB,
        web_user=request.user,
        status=AgentJob.Status.FAILED,
    )
    if job.thread_id is None or is_thread_stale(job.thread):
        if job.thread_id is not None and job.thread.closed_at is None:
            job.thread.closed_at = timezone.now()
            job.thread.save(update_fields=["closed_at"])
        response = HttpResponse("Please send your message again.", status=409, content_type="text/plain; charset=utf-8")
        return _with_toast(response, "Please send your message again.", "error")

    AgentJob.objects.filter(pk=job.pk, status=AgentJob.Status.FAILED).update(
        status=AgentJob.Status.QUEUED,
        attempts=0,
        error="",
        started_at=None,
        finished_at=None,
    )
    if _wants_fragment(request):
        return render(request, "web/_ask_typing.html")
    return redirect("web:ask")


def _decide_web_proposal(request, pk: int, *, confirm: bool):
    _require_web_chat_enabled()
    account = _web_account_for(request.user)
    if account is None:
        return HttpResponse("Ask isn't set up for your account yet.", status=403)

    proposal = AgentProposal.objects.select_related("thread").filter(pk=pk).first()
    if (
        proposal is None
        or proposal.thread.channel != AgentThread.Channel.WEB
        or proposal.thread.web_user_id != request.user.id
        or proposal.role != account.role
        or proposal.thread.closed_at is not None
    ):
        raise Http404

    if proposal.status != AgentProposal.Status.PENDING:
        return HttpResponse("This proposal has already been decided.", status=409)

    if confirm and not settings.AGENT_PROPOSALS_ENABLED:
        return HttpResponse("Proposals are currently disabled.", status=403)

    if not decide(proposal, confirm=confirm):
        return HttpResponse("This proposal has already been decided.", status=409)

    proposal.refresh_from_db()
    card = present_proposal(proposal)
    level = "error" if card["is_failed"] else ("success" if card["is_confirmed"] else "info")
    if _wants_fragment(request):
        response = render(request, "web/_ask_proposal_card.html", {"card": card})
        return _with_toast(response, card["status_text"], level)
    messages.add_message(
        request,
        messages.ERROR if level == "error" else (messages.SUCCESS if level == "success" else messages.INFO),
        card["status_text"],
    )
    return redirect("web:ask")


@login_required
@require_POST
def ask_proposal_confirm(request, pk):
    return _decide_web_proposal(request, pk, confirm=True)


@login_required
@require_POST
def ask_proposal_cancel(request, pk):
    return _decide_web_proposal(request, pk, confirm=False)


@login_required
@require_POST
def ask_proposals_confirm_all(request):
    _require_web_chat_enabled()
    account = _web_account_for(request.user)
    if account is None:
        return HttpResponse("Ask isn't set up for your account yet.", status=403)
    if not settings.AGENT_PROPOSALS_ENABLED:
        return HttpResponse("Proposals are currently disabled.", status=403)

    raw_ids = [int(v) for v in request.POST.getlist("proposal_id") if v.isdigit()]
    if not raw_ids:
        return HttpResponse("No proposals supplied.", status=400)

    thread = open_thread_for(request.user)
    if thread is None:
        raise Http404

    proposals = list(
        AgentProposal.objects.filter(
            pk__in=raw_ids,
            thread=thread,
            role=account.role,
        ).order_by("id")
    )
    if not proposals:
        raise Http404

    confirmed = 0
    for proposal in proposals:
        if proposal.status == AgentProposal.Status.PENDING and decide(proposal, confirm=True):
            confirmed += 1
        proposal.refresh_from_db()

    cards = [present_proposal(p) for p in proposals]
    pending_ids = [c["id"] for c in cards if c["is_pending"]]
    group_id = request.POST.get("group_id") or f"proposals-batch-{proposals[0].pk}"
    toast = f"Added {confirmed} item{'s' if confirmed != 1 else ''}." if confirmed else "Already decided."
    if _wants_fragment(request):
        response = render(request, "web/_ask_proposal_group.html", {
            "cards": cards,
            "pending_ids": pending_ids,
            "group_id": group_id,
        })
        return _with_toast(response, toast, "success" if confirmed else "info")
    messages.success(request, toast)
    return redirect("web:ask")


@login_required
@require_GET
def ask_document_download(request, pk):
    _require_web_chat_enabled()
    account = _web_account_for(request.user)
    if account is None:
        return HttpResponse("Ask isn't set up for your account yet.", status=403)

    doc = get_object_or_404(GeneratedDocument, pk=pk, role=account.role)
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', "_", doc.filename or "document.docx")
    response = HttpResponse(bytes(doc.data), content_type=DOCX_MIME)
    response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response
