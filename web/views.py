from django.http import JsonResponse


def healthz(request):
    """Simple liveness check. No auth required."""
    return JsonResponse({"status": "ok"})
