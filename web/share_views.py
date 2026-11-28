"""The iPhone share-sheet endpoint: an Apple Shortcut posts files, photos or
text plus an optional instruction, and they land in the sender's Ask chat.

This is the one Ask endpoint without a login session. The Shortcut proves who
it is with a per-person share key (see WebAccount.issue_share_key) in the
Authorization header, so the endpoint is exempt from CSRF. A key can only
queue messages: confirming anything the agent proposes still needs a tap in
the signed-in web app.

Replies are short plain text, because the Shortcut shows the response body
as a notification, whatever the status code.
"""

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from assistant.agent.proposals import ToolInputError
from assistant.agent.web import enqueue_shared_items
from core.models import AuditLog, WebAccount

INVALID_KEY = "That share key isn't valid. Make a new one in LifeOS Settings and paste it into the Shortcut."


def _text(body: str, status: int = 200) -> HttpResponse:
    response = HttpResponse(body, status=status, content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "no-store"
    return response


def _bearer_key(request) -> str:
    header = request.headers.get("Authorization", "").strip()
    if header[:7].lower() == "bearer ":
        header = header[7:].strip()
    return header


@csrf_exempt
@require_POST
def ask_share(request):
    if not settings.AGENT_WEB_CHAT_ENABLED or not settings.AGENT_ENABLED:
        return _text("Sharing to LifeOS is switched off right now.", 503)

    account = WebAccount.for_share_key(_bearer_key(request))
    if account is None or not account.user.is_active:
        return _text(INVALID_KEY, 401)

    files = [f for name in request.FILES for f in request.FILES.getlist(name)]
    # Shortcuts sends shared text as a plain form value when the input isn't
    # a file, so a non-file "file" field is treated as the shared text.
    shared_text = "\n\n".join(v for v in request.POST.getlist("file") if v.strip())
    try:
        jobs = enqueue_shared_items(
            user=account.user,
            account=account,
            text=request.POST.get("message", ""),
            files=files,
            shared_text=shared_text,
        )
    except ToolInputError as exc:
        return _text(str(exc), 400)

    account.share_key_last_used_at = timezone.now()
    account.save(update_fields=["share_key_last_used_at"])
    AuditLog.objects.create(
        action="agent_share_received",
        object_type="AgentJob",
        object_id=str(jobs[0].pk),
        details={"role": account.role, "jobs": len(jobs), "files": len(files)},
    )
    if files:
        what = "file" if len(files) == 1 else f"{len(files)} files"
        if len(files) == 1 and jobs[0].reply_metadata.get("attachment_kind") == "image":
            what = "photo"
        return _text(f"Sent your {what} to LifeOS. Open Ask to see the reply.")
    return _text("Sent to LifeOS. Open Ask to see the reply.")
