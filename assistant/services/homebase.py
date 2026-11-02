"""Small, dependency-free client for Homebase, the household's home manager.

Homebase is a Supabase project. LifeOS signs in as its own Homebase account,
so Homebase's row-level security decides what it can read; this module never
holds a service-role key. The password is kept in Django settings and never
appears in errors or logs.

Supabase rotates refresh tokens on use, so instead of storing one this signs
in again with the password whenever the cached access token runs out.
"""

import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings

DOCUMENT_BUCKET = "documents"
TIMEOUT_SECONDS = 20
# Sign in again this long before the access token would expire.
TOKEN_MARGIN_SECONDS = 60
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_token_lock = threading.Lock()
_token = {"value": "", "expires_at": 0.0}


class HomebaseError(RuntimeError):
    pass


class HomebaseFileTooLargeError(HomebaseError):
    pass


def configured() -> bool:
    return all([
        settings.HOMEBASE_SUPABASE_URL, settings.HOMEBASE_SUPABASE_ANON_KEY,
        settings.HOMEBASE_AGENT_EMAIL, settings.HOMEBASE_AGENT_PASSWORD,
    ])


def _url(path: str, params: dict | None = None) -> str:
    url = settings.HOMEBASE_SUPABASE_URL.rstrip("/") + path
    return f"{url}?{urlencode(params)}" if params else url


def _open(request: Request, *, what: str, max_bytes: int | None = None) -> bytes:
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - configured Supabase host
            data = response.read(max_bytes + 1 if max_bytes else -1)
    except HTTPError as exc:
        # Do not chain urllib errors: the sign-in request body holds the password.
        raise HomebaseError(f"Homebase {what} failed with HTTP {exc.code}.") from None
    except (URLError, TimeoutError):
        raise HomebaseError(f"Homebase could not be reached for {what}.") from None
    if max_bytes and len(data) > max_bytes:
        raise HomebaseFileTooLargeError(f"The Homebase file is larger than {max_bytes // (1024 * 1024)} MB.")
    return data


def _sign_in() -> tuple[str, float]:
    body = json.dumps({
        "email": settings.HOMEBASE_AGENT_EMAIL, "password": settings.HOMEBASE_AGENT_PASSWORD,
    }).encode("utf-8")
    request = Request(
        _url("/auth/v1/token", {"grant_type": "password"}), data=body, method="POST",
        headers={"apikey": settings.HOMEBASE_SUPABASE_ANON_KEY, "Content-Type": "application/json"},
    )
    try:
        session = json.loads(_open(request, what="sign-in"))
        return session["access_token"], time.monotonic() + int(session.get("expires_in", 3600))
    except (ValueError, KeyError, TypeError):
        raise HomebaseError("Homebase sign-in returned an unexpected response.") from None


def _access_token(*, fresh: bool = False) -> str:
    with _token_lock:
        if fresh or not _token["value"] or time.monotonic() >= _token["expires_at"] - TOKEN_MARGIN_SECONDS:
            _token["value"], _token["expires_at"] = _sign_in()
        return _token["value"]


def _authorised(url: str, *, what: str, max_bytes: int | None = None) -> bytes:
    if not configured():
        raise HomebaseError("Homebase is not configured.")
    for fresh in (False, True):
        request = Request(url, headers={
            "apikey": settings.HOMEBASE_SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {_access_token(fresh=fresh)}",
            "Accept": "application/json" if max_bytes is None else "*/*",
        })
        try:
            return _open(request, what=what, max_bytes=max_bytes)
        except HomebaseError as exc:
            # A token revoked early (password change, sign-out elsewhere) gets one retry.
            if fresh or "HTTP 401" not in str(exc):
                raise
    raise AssertionError("unreachable")


def select(table: str, *, columns: str, filters: dict | None = None, order: str = "", limit: int = 100) -> list[dict]:
    """Rows from one Homebase table, filtered with PostgREST operators ("eq.open")."""
    params = {"select": columns, "limit": str(limit), **(filters or {})}
    if order:
        params["order"] = order
    try:
        rows = json.loads(_authorised(_url(f"/rest/v1/{table}", params), what=f"read of {table}"))
    except ValueError:
        raise HomebaseError(f"Homebase returned unreadable {table} data.") from None
    if not isinstance(rows, list):
        raise HomebaseError(f"Homebase returned unexpected {table} data.")
    return rows


def storage_path(value: str | None, bucket: str) -> str | None:
    """The object path inside ``bucket`` for a stored path or Supabase storage URL.

    Mirrors normalizeStoragePath in Homebase's src/lib/storage.ts.
    """
    if not value:
        return None
    if not re.match(r"https?://", value, re.IGNORECASE):
        return value
    path = urlsplit(value).path
    for kind in ("public", "sign", "authenticated"):
        marker = f"/storage/v1/object/{kind}/{bucket}/"
        if marker in path:
            return unquote(path.split(marker, 1)[1])
    return None


def download(bucket: str, path: str, *, max_bytes: int) -> bytes:
    return _authorised(
        _url(f"/storage/v1/object/authenticated/{bucket}/{quote(path)}"),
        what="file download", max_bytes=max_bytes,
    )
