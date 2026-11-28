from urllib.parse import urljoin

from django.conf import settings
from django.templatetags.static import static

from core.models import ParsedAction, WebAccount


def pending_review_count(request):
    """Exposes {{ pending_review_count }} in every template for the nav
    badge. Every household member may see the count (per Phase 3 spec);
    only staff can act on it — enforced in the views, not here."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {
        "pending_review_count": ParsedAction.objects.filter(
            status=ParsedAction.Status.PENDING_REVIEW
        ).count()
    }


def ask_navigation(request):
    """Whether the Ask chat screen is enabled and set up for this login."""
    if not settings.AGENT_WEB_CHAT_ENABLED or not settings.AGENT_ENABLED:
        return {"ask_available": False}
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"ask_available": False}
    return {"ask_available": WebAccount.objects.filter(user=user).exists()}


def site_metadata(request):
    """Absolute, query-free URLs for canonical and social metadata.

    APP_BASE_URL keeps production metadata on the public HTTPS origin even
    when Django is reached through a reverse proxy. Local development falls
    back to the current request origin.
    """
    configured_base_url = getattr(settings, "APP_BASE_URL", "").strip().rstrip("/")
    base_url = configured_base_url or f"{request.scheme}://{request.get_host()}"

    return {
        "canonical_url": f"{base_url}{request.path}",
        "social_image_url": urljoin(
            f"{base_url}/", static("images/og-lifeos.jpg")
        ),
    }
