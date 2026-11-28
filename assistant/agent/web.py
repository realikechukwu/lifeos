"""Web channel ("Ask") for the agent: queueing from the web UI, thread
selection, per-turn context, and running turns in the worker."""

import base64
from datetime import timedelta
from pathlib import PurePosixPath

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import (
    AgentJob,
    AgentMemory,
    AgentMessage,
    AgentOutcome,
    AgentThread,
    AuditLog,
    HouseholdMember,
    WebAccount,
)

from .documents import (
    MAX_UPLOAD_BYTES,
    PDF_MIME,
    SUPPORTED_UPLOADS,
    UPLOAD_LABELS,
    add_upload,
    upload_context,
    upload_mime,
)
from .loop import PROMPT_VERSION, append_input, run_turn
from .photos import DEFAULT_CAPTION, IMAGE_MIMES, MAX_PHOTO_BYTES
from .proposals import ToolInputError, context_outcomes
from .tools import ToolContext

IMAGE_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def speaker_name(account: WebAccount) -> str:
    if account.role == HouseholdMember.Role.IKE:
        return "Ike"
    if account.display_name.strip():
        return account.display_name.strip()
    if account.user.first_name.strip():
        return account.user.first_name.strip()
    member = HouseholdMember.objects.filter(role=account.role, active=True).first()
    if member and member.name.strip():
        return member.name.strip()
    return "Ike's wife"


def greeting_name(account: WebAccount) -> str:
    """Short name for the empty-state greeting ("Morning, Ike"), or empty."""
    if account.display_name.strip():
        return account.display_name.strip()
    if account.user.first_name.strip():
        return account.user.first_name.strip()
    if account.role == HouseholdMember.Role.IKE:
        return "Ike"
    member = HouseholdMember.objects.filter(role=account.role, active=True).first()
    if member and member.name.strip():
        return member.name.strip().split()[0]
    return ""


def is_thread_stale(thread: AgentThread, *, now=None) -> bool:
    """Whether a thread is closed, past the idle limit, or on an older prompt."""
    now = now or timezone.now()
    idle_limit = timedelta(hours=settings.AGENT_THREAD_IDLE_HOURS)
    return (
        thread.closed_at is not None
        or now - thread.last_activity_at > idle_limit
        or thread.prompt_version != PROMPT_VERSION
    )


def open_thread_for(user) -> AgentThread | None:
    """Return the user's active web thread, closing it if idle or outdated."""
    now = timezone.now()
    thread = (
        AgentThread.objects.filter(
            channel=AgentThread.Channel.WEB,
            web_user=user,
            closed_at__isnull=True,
        )
        .order_by("-last_activity_at")
        .first()
    )
    if thread and is_thread_stale(thread, now=now):
        thread.closed_at = now
        thread.save(update_fields=["closed_at"])
        return None
    return thread


def thread_for(user, role: str) -> AgentThread:
    """Return one open web thread per user, rotating when idle or outdated."""
    thread = open_thread_for(user)
    if thread is None:
        thread = AgentThread.objects.create(
            channel=AgentThread.Channel.WEB,
            role=role,
            web_user=user,
            last_activity_at=timezone.now(),
            prompt_version=PROMPT_VERSION,
        )
    return thread


def close_thread(user) -> bool:
    """Close any open web thread for this user (used by New chat)."""
    return bool(
        AgentThread.objects.filter(
            channel=AgentThread.Channel.WEB,
            web_user=user,
            closed_at__isnull=True,
        ).update(closed_at=timezone.now())
    )


def build_context_note(
    *,
    thread: AgentThread,
    account: WebAccount,
    first_turn: bool,
    outcome_note: str | None = None,
    upload_note: str = "",
) -> str:
    """Trusted per-turn facts, sent as a system message after the user's words."""
    now = timezone.localtime()
    speaker = speaker_name(account)
    lines = [
        f"Current time: {now:%A %-d %B %Y, %H:%M} (Europe/London).",
        f"Speaker: {speaker} (household role: {account.role}), writing in a private web chat in the LifeOS web app.",
    ]
    if first_turn:
        memories = list(AgentMemory.objects.filter(role=account.role, active=True))
        if memories:
            lines.append(f"Saved memories for {speaker}:")
            lines.extend(f"- [{m.id}] {m.text}" for m in memories)
        else:
            lines.append(f"No saved memories for {speaker} yet.")
    if outcome_note is None:
        outcome_note, _ = context_outcomes(thread)
    if outcome_note:
        lines.append(outcome_note)
    if upload_note:
        lines.append(upload_note)
    return "\n".join(lines)


def _image_mime(filename: str, content_type: str) -> str | None:
    mime = str(content_type or "").split(";")[0].strip().lower()
    if mime in IMAGE_MIMES:
        return mime
    if mime in {"", "application/octet-stream"}:
        return IMAGE_EXTENSIONS.get(PurePosixPath(str(filename or "")).suffix.lower())
    return None


def enqueue_web_message(*, user, account: WebAccount, text: str, uploaded_file=None) -> AgentJob:
    """Validate and queue a web chat message (with optional photo or document)."""
    if not settings.AGENT_ENABLED:
        raise ToolInputError("The LifeOS assistant is currently switched off.")
    clean_text = (text or "").strip()[:8000]
    if not clean_text and uploaded_file is None:
        raise ToolInputError("Write a message or attach a file.")

    if uploaded_file is None:
        return AgentJob.objects.create(
            channel=AgentThread.Channel.WEB,
            web_user=user,
            text=clean_text,
        )

    filename = str(getattr(uploaded_file, "name", "") or "upload")[:255]
    content_type = str(getattr(uploaded_file, "content_type", "") or "")
    img_mime = _image_mime(filename, content_type)
    if img_mime:
        if not settings.AGENT_PHOTOS_ENABLED:
            raise ToolInputError("Photo uploads are currently switched off.")
        if getattr(uploaded_file, "size", 0) > MAX_PHOTO_BYTES:
            raise ToolInputError("That image is over 5 MB. Send a smaller image.")
        data = uploaded_file.read(MAX_PHOTO_BYTES + 1)
        if not data:
            raise ToolInputError("That image file is empty.")
        if len(data) > MAX_PHOTO_BYTES:
            raise ToolInputError("That image is over 5 MB. Send a smaller image.")
        caption = clean_text or DEFAULT_CAPTION
        with transaction.atomic():
            job = AgentJob.objects.create(
                channel=AgentThread.Channel.WEB,
                web_user=user,
                text=caption,
                input_blocks=[
                    {"type": "text", "text": caption},
                    {"type": "text", "text": "Untrusted photo sent by the speaker:"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img_mime,
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    },
                ],
                reply_metadata={"attachment_name": filename, "attachment_kind": "image"},
            )
            AuditLog.objects.create(
                action="agent_photo_received",
                object_type="AgentJob",
                object_id=str(job.pk),
                details={"role": account.role, "channel": AgentThread.Channel.WEB},
            )
        return job

    doc_mime = upload_mime(filename, content_type)
    if doc_mime is None:
        raise ToolInputError(f"Send a photo (up to 5 MB) or {SUPPORTED_UPLOADS} (up to 10 MB).")
    if not settings.AGENT_DOCUMENTS_ENABLED:
        raise ToolInputError("Document uploads are currently switched off.")
    if getattr(uploaded_file, "size", 0) > MAX_UPLOAD_BYTES:
        raise ToolInputError(f"Upload {SUPPORTED_UPLOADS} no larger than 10 MB.")
    data = uploaded_file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ToolInputError(f"Upload {SUPPORTED_UPLOADS} no larger than 10 MB.")
    caption = clean_text or "Please read this uploaded document."
    with transaction.atomic():
        job = AgentJob.objects.create(
            channel=AgentThread.Channel.WEB,
            web_user=user,
            text=caption,
            reply_metadata={"attachment_name": filename, "attachment_kind": "document"},
        )
        upload = add_upload(job, role=account.role, filename=filename, mime_type=doc_mime, data=data)
        blocks = [
            {"type": "text", "text": caption},
            {"type": "text", "text": f"Untrusted uploaded document: {upload.filename}"},
        ]
        if doc_mime == PDF_MIME:
            blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": PDF_MIME,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            })
        else:
            blocks.append({
                "type": "text",
                "text": f"Untrusted {UPLOAD_LABELS[doc_mime]} extracted text:\n" + upload.text,
            })
        job.input_blocks = blocks
        job.save(update_fields=["input_blocks"])
    return job


def process_web_jobs(jobs: list[AgentJob], *, client=None) -> None:
    """Answer one or more queued web messages from the same user as one turn."""
    job = jobs[0]
    if not settings.AGENT_WEB_CHAT_ENABLED or not settings.AGENT_ENABLED:
        raise ValueError("Web chat has been disabled; job was not executed.")
    user = job.web_user
    account = WebAccount.objects.filter(user=user).select_related("user").first() if user else None
    if account is None:
        raise ValueError("The web user does not have a linked household WebAccount.")

    if job.thread_id is not None:
        # A retry: this turn's input is already in the thread.
        thread = job.thread
    else:
        thread = thread_for(user, account.role)
        first_turn = not thread.messages.exists()
        text = "\n\n".join(j.text for j in jobs if j.text.strip())
        blocks = []
        for item in jobs:
            blocks.extend(item.input_blocks or [{"type": "text", "text": item.text}])
        user_content = blocks if any(j.input_blocks for j in jobs) else [{"type": "text", "text": text}]
        upload_note = upload_context(
            ToolContext(
                role=account.role,
                speaker_name=speaker_name(account),
                thread=thread,
                job=job,
                channel=AgentThread.Channel.WEB,
                web_user=user,
            )
        )
        outcome_note, outcome_ids = context_outcomes(thread)
        system_content = build_context_note(
            thread=thread,
            account=account,
            first_turn=first_turn,
            outcome_note=outcome_note,
            upload_note=upload_note,
        )
        batch_metadata = {**job.reply_metadata, "batch_id": job.pk}
        with transaction.atomic():
            append_input(thread, [
                {"role": AgentMessage.Role.USER, "content": user_content},
                {"role": AgentMessage.Role.SYSTEM, "content": system_content},
            ])
            AgentOutcome.objects.filter(id__in=outcome_ids).update(delivered_at=timezone.now())
            AgentJob.objects.filter(id__in=[j.id for j in jobs]).update(thread=thread)
            AgentJob.objects.filter(pk=job.pk).update(reply_metadata=batch_metadata)
            for item in jobs[1:]:
                AgentJob.objects.filter(pk=item.pk).update(
                    reply_metadata={**item.reply_metadata, "batch_id": job.pk}
                )
        job.thread = thread
        job.reply_metadata = batch_metadata

    ctx = ToolContext(
        role=account.role,
        speaker_name=speaker_name(account),
        thread=thread,
        job=job,
        channel=AgentThread.Channel.WEB,
        web_user=user,
    )
    if not job.reply_metadata.get("model_finished"):
        result = run_turn(thread, ctx, client=client)
        job.reply_text = result.text
        last_msg_id = (
            thread.messages.filter(role=AgentMessage.Role.ASSISTANT)
            .order_by("-id")
            .values_list("id", flat=True)
            .first()
        )
        doc_ids = list(job.document_deliveries.values_list("document_id", flat=True))
        job.reply_metadata = {
            **job.reply_metadata,
            "model_finished": True,
            "proposal_ids": ctx.proposal_ids,
            "document_ids": doc_ids,
            "assistant_message_id": last_msg_id,
        }
        AgentJob.objects.filter(pk=job.pk).update(reply_text=job.reply_text, reply_metadata=job.reply_metadata)
        job.document_deliveries.filter(sent_at__isnull=True).update(sent_at=timezone.now())
