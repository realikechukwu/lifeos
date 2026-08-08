"""Public HTTP endpoints owned by assistant integrations."""

import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.models import TelegramUpdate

from .services.telegram_handlers import process_telegram_update

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def telegram_webhook(request):
    """Receive Telegram updates authenticated with Telegram's secret header."""
    expected = settings.TELEGRAM_WEBHOOK_SECRET
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected:
        return JsonResponse({"detail": "Telegram is not configured."}, status=503)
    if not hmac.compare_digest(supplied, expected):
        return HttpResponse(status=403)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"detail": "Invalid JSON."}, status=400)
    try:
        process_telegram_update(payload)
    except Exception as exc:  # noqa: BLE001 - return 500 so Telegram safely retries
        update_id = payload.get("update_id")
        if isinstance(update_id, int):
            TelegramUpdate.objects.filter(update_id=update_id).update(error=str(exc)[:2000])
        logger.exception("Telegram webhook processing failed update_id=%s", update_id)
        return JsonResponse({"detail": "Update processing failed."}, status=500)
    return JsonResponse({"ok": True})
