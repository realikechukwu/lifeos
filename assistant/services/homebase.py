"""Small, dependency-free client for Homebase, the household's home manager.

Homebase is a Supabase project. LifeOS signs in as its own Homebase account,
so Homebase's row-level security decides what it can do; this module never
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
SNAG_BUCKET = "snag-photos"
TIMEOUT_SECONDS = 20
# Sign in again this long before the access token would expire.
TOKEN_MARGIN_SECONDS = 60
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_token_lock = threading.Lock()
_token = {"value": "", "expires_at": 0.0, "user_id": ""}


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
            # Never read(-1): on a chunked response it returns the chunk framing too.
            data = response.read(max_bytes + 1) if max_bytes else response.read()
    except HTTPError as exc:
        # Do not chain urllib errors: the sign-in request body holds the password.
        raise HomebaseError(f"Homebase {what} failed with HTTP {exc.code}{_reason(exc)}.") from None
    except (URLError, TimeoutError):
        raise HomebaseError(f"Homebase could not be reached for {what}.") from None
    if max_bytes and len(data) > max_bytes:
        raise HomebaseFileTooLargeError(f"The Homebase file is larger than {max_bytes // (1024 * 1024)} MB.")
    return data


def _reason(exc: HTTPError) -> str:
    """Supabase's own error code and message; auth errors never echo the password."""
    try:
        body = json.loads(exc.read(2000))
        parts = [body.get("error_code") or body.get("code"), body.get("msg") or body.get("message")]
    except (ValueError, AttributeError, TypeError, OSError):
        return ""
    reason = ": ".join(str(p) for p in parts if p)[:200]
    return f" ({reason})" if reason else ""


def _sign_in() -> tuple[str, float, str]:
    body = json.dumps({
        "email": settings.HOMEBASE_AGENT_EMAIL, "password": settings.HOMEBASE_AGENT_PASSWORD,
    }).encode("utf-8")
    request = Request(
        _url("/auth/v1/token", {"grant_type": "password"}), data=body, method="POST",
        headers={"apikey": settings.HOMEBASE_SUPABASE_ANON_KEY, "Content-Type": "application/json"},
    )
    try:
        session = json.loads(_open(request, what="sign-in"))
        expires_at = time.monotonic() + int(session.get("expires_in", 3600))
        return session["access_token"], expires_at, session["user"]["id"]
    except (ValueError, KeyError, TypeError):
        raise HomebaseError("Homebase sign-in returned an unexpected response.") from None


def _access_token(*, fresh: bool = False) -> str:
    with _token_lock:
        if fresh or not _token["value"] or time.monotonic() >= _token["expires_at"] - TOKEN_MARGIN_SECONDS:
            _token["value"], _token["expires_at"], _token["user_id"] = _sign_in()
        return _token["value"]


def user_id() -> str:
    """The Homebase profile id of the LifeOS account, for reported_by."""
    _access_token()
    return _token["user_id"]


def _authorised(url: str, *, what: str, method: str = "GET", body: bytes | None = None,
                headers: dict | None = None, max_bytes: int | None = None) -> bytes:
    if not configured():
        raise HomebaseError("Homebase is not configured.")
    for fresh in (False, True):
        request = Request(url, data=body, method=method, headers={
            "apikey": settings.HOMEBASE_SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {_access_token(fresh=fresh)}",
            "Accept": "application/json" if max_bytes is None else "*/*",
            **(headers or {}),
        })
        try:
            return _open(request, what=what, max_bytes=max_bytes)
        except HomebaseError as exc:
            # A token revoked early (password change, sign-out elsewhere) gets
            # one retry; Supabase rejects those before running anything.
            if fresh or "HTTP 401" not in str(exc):
                raise
    raise AssertionError("unreachable")


def _rows(data: bytes, table: str) -> list[dict]:
    try:
        rows = json.loads(data)
    except ValueError:
        raise HomebaseError(f"Homebase returned unreadable {table} data.") from None
    if not isinstance(rows, list):
        raise HomebaseError(f"Homebase returned unexpected {table} data.")
    return rows


def select(table: str, *, columns: str, filters: dict | None = None, order: str = "", limit: int = 100) -> list[dict]:
    """Rows from one Homebase table, filtered with PostgREST operators ("eq.open")."""
    params = {"select": columns, "limit": str(limit), **(filters or {})}
    if order:
        params["order"] = order
    return _rows(_authorised(_url(f"/rest/v1/{table}", params), what=f"read of {table}"), table)


def _write(table: str, method: str, params: dict | None, values: dict) -> list[dict]:
    data = _authorised(
        _url(f"/rest/v1/{table}", params), what=f"save to {table}", method=method,
        body=json.dumps(values).encode("utf-8"),
        headers={"Content-Type": "application/json", "Prefer": "return=representation"},
    )
    return _rows(data, table)


def insert(table: str, values: dict) -> dict:
    rows = _write(table, "POST", None, values)
    if len(rows) != 1:
        raise HomebaseError(f"Homebase did not confirm the new {table} row.")
    return rows[0]


def update(table: str, filters: dict, values: dict) -> list[dict]:
    """Update the rows matching ``filters``; returns them, or [] when none matched."""
    return _write(table, "PATCH", filters, values)


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


def upload(bucket: str, path: str, data: bytes, *, content_type: str) -> None:
    _authorised(
        _url(f"/storage/v1/object/{bucket}/{quote(path)}"), what="file upload", method="POST",
        body=data, headers={"Content-Type": content_type},
    )


def remove(bucket: str, path: str) -> None:
    _authorised(_url(f"/storage/v1/object/{bucket}/{quote(path)}"), what="file removal", method="DELETE")


def download(bucket: str, path: str, *, max_bytes: int) -> bytes:
    return _authorised(
        _url(f"/storage/v1/object/authenticated/{bucket}/{quote(path)}"),
        what="file download", max_bytes=max_bytes,
    )
