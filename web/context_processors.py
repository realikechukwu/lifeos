from core.models import ParsedAction


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
