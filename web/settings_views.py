"""Settings: who is signed in, links for admins, and the key that lets the
iPhone share-sheet Shortcut send things to this person's Ask chat."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from assistant.agent.web import greeting_name
from core.models import AuditLog, WebAccount

NEW_KEY_SESSION = "lifeos_new_share_key"


def _account_for(user) -> WebAccount | None:
    return WebAccount.objects.filter(user=user).select_related("user").first()


def _share_available(account: WebAccount | None) -> bool:
    return bool(account) and settings.AGENT_WEB_CHAT_ENABLED and settings.AGENT_ENABLED


def _share_url(request) -> str:
    path = reverse("web:ask_share")
    base = getattr(settings, "APP_BASE_URL", "").strip().rstrip("/")
    return f"{base}{path}" if base else request.build_absolute_uri(path)


@login_required
@require_GET
def settings_page(request):
    account = _account_for(request.user)
    # A new key is shown once: it is popped here, right after the redirect
    # that follows making it, and never stored anywhere else in plain text.
    new_key = request.session.pop(NEW_KEY_SESSION, "")
    response = render(request, "web/settings.html", {
        "account": account,
        "display_name": (greeting_name(account) if account else "") or request.user.get_username(),
        "share_available": _share_available(account),
        "share_url": _share_url(request),
        "new_key": new_key,
    })
    if new_key:
        response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def share_key_create(request):
    account = _account_for(request.user)
    if not _share_available(account):
        messages.error(request, "Sharing from iPhone isn't available for your login yet.")
        return redirect("web:settings")
    replacing = account.has_share_key
    request.session[NEW_KEY_SESSION] = account.issue_share_key()
    AuditLog.objects.create(
        action="share_key_replaced" if replacing else "share_key_created",
        object_type="WebAccount",
        object_id=str(account.pk),
        details={"role": account.role},
    )
    return redirect(f"{reverse('web:settings')}#share")


@login_required
@require_POST
def share_key_revoke(request):
    account = _account_for(request.user)
    if account and account.has_share_key:
        account.revoke_share_key()
        AuditLog.objects.create(
            action="share_key_revoked",
            object_type="WebAccount",
            object_id=str(account.pk),
            details={"role": account.role},
        )
        messages.success(request, "Share key turned off. The Shortcut stops working until you make a new key.")
    return redirect(f"{reverse('web:settings')}#share")
