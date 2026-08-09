"""One-way Patchwork iCalendar synchronisation for Ike's work shifts.

The Patchwork subscription URL is a private bearer credential.  This module
never logs it and deliberately imports only timing/status information: the
shared calendar always displays the neutral title ``Ike work shift``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone
from googleapiclient.discovery import build

from core.models import PatchworkShift

from .gmail import get_google_credentials


WORK_SHIFT_TITLE = "Ike work shift"
WORK_SHIFT_DESCRIPTION = "Synced automatically from Patchwork. Edit this shift in Patchwork."


@dataclass(frozen=True)
class PatchworkShiftData:
    uid: str
    starts_at: datetime
    ends_at: datetime | None
    timezone_name: str
    all_day: bool
    status: str
    payload_hash: str


@dataclass(frozen=True)
class SyncResult:
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    unchanged: int = 0


def fetch_patchwork_calendar() -> bytes:
    """Fetch the iCalendar bytes without exposing the subscription URL."""
    url = settings.PATCHWORK_CALENDAR_URL.strip()
    if not url:
        raise RuntimeError("PATCHWORK_CALENDAR_URL is not configured.")
    request = Request(url, headers={"Accept": "text/calendar"})
    with urlopen(request, timeout=settings.PATCHWORK_CALENDAR_TIMEOUT_SECONDS) as response:  # noqa: S310 - configured HTTPS feed
        payload = response.read()
    if not payload:
        raise ValueError("Patchwork returned an empty calendar response.")
    return payload


def _unfold_ical_lines(raw_calendar: bytes) -> list[str]:
    try:
        lines = raw_calendar.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("Patchwork calendar is not valid UTF-8.") from exc

    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _parse_property(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    left, value = line.split(":", 1)
    pieces = left.split(";")
    name = pieces[0].upper()
    params: dict[str, str] = {}
    for part in pieces[1:]:
        if "=" not in part:
            continue
        key, param_value = part.split("=", 1)
        params[key.upper()] = param_value.strip('"')
    return name, params, value


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(settings.APP_TIMEZONE)


def _parse_ical_datetime(value: str, params: dict[str, str]) -> tuple[datetime, bool, str]:
    value = value.strip()
    timezone_name = params.get("TZID", settings.APP_TIMEZONE)
    if params.get("VALUE", "").upper() == "DATE" or len(value) == 8:
        parsed_date = datetime.strptime(value, "%Y%m%d").date()
        return datetime.combine(parsed_date, datetime.min.time(), _zone(timezone_name)), True, timezone_name

    if value.endswith("Z"):
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime_timezone.utc)
        return parsed.astimezone(_zone(timezone_name)), False, timezone_name

    parsed = datetime.strptime(value, "%Y%m%dT%H%M%S")
    return parsed.replace(tzinfo=_zone(timezone_name)), False, timezone_name


def _hash_shift(*, starts_at: datetime, ends_at: datetime | None, timezone_name: str, all_day: bool, status: str) -> str:
    values = (starts_at.isoformat(), ends_at.isoformat() if ends_at else "", timezone_name, str(all_day), status)
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def parse_patchwork_calendar(raw_calendar: bytes) -> list[PatchworkShiftData]:
    """Parse the event fields used for synchronisation from an RFC 5545 feed."""
    events: list[dict[str, tuple[dict[str, str], str]]] = []
    current: dict[str, tuple[dict[str, str], str]] | None = None
    vevent_count = 0

    for line in _unfold_ical_lines(raw_calendar):
        if line == "BEGIN:VEVENT":
            current = {}
            vevent_count += 1
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None:
            continue
        parsed = _parse_property(line)
        if parsed:
            name, params, value = parsed
            current[name] = (params, value)

    shifts: list[PatchworkShiftData] = []
    seen_uids: set[str] = set()
    for event in events:
        uid = event.get("UID", ({}, ""))[1].strip()
        starts = event.get("DTSTART")
        if not uid or not starts or uid in seen_uids:
            continue
        seen_uids.add(uid)
        start_params, start_value = starts
        try:
            starts_at, all_day, timezone_name = _parse_ical_datetime(start_value, start_params)
            ends_at = None
            if ends := event.get("DTEND"):
                ends_at, end_is_all_day, _ = _parse_ical_datetime(ends[1], ends[0])
                if end_is_all_day != all_day or ends_at <= starts_at:
                    raise ValueError("invalid end time")
        except ValueError as exc:
            raise ValueError("Patchwork calendar contains an invalid shift time.") from exc

        status = event.get("STATUS", ({}, "CONFIRMED"))[1].strip().upper() or "CONFIRMED"
        shifts.append(PatchworkShiftData(
            uid=uid,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone_name=timezone_name,
            all_day=all_day,
            status=status,
            payload_hash=_hash_shift(
                starts_at=starts_at,
                ends_at=ends_at,
                timezone_name=timezone_name,
                all_day=all_day,
                status=status,
            ),
        ))

    if vevent_count and not shifts:
        raise ValueError("Patchwork calendar contained no usable shift events.")
    return shifts


def _google_event_body(shift: PatchworkShiftData) -> dict:
    body = {
        "summary": WORK_SHIFT_TITLE,
        "description": WORK_SHIFT_DESCRIPTION,
        "extendedProperties": {"private": {"lifeos_source": "patchwork", "patchwork_uid": shift.uid}},
    }
    if shift.all_day:
        start_date = shift.starts_at.date()
        end_date = shift.ends_at.date() if shift.ends_at else start_date + timedelta(days=1)
        body["start"] = {"date": start_date.isoformat()}
        body["end"] = {"date": end_date.isoformat()}
    else:
        body["start"] = {"dateTime": shift.starts_at.isoformat(), "timeZone": shift.timezone_name}
        if shift.ends_at:
            body["end"] = {"dateTime": shift.ends_at.isoformat(), "timeZone": shift.timezone_name}
    return body


def _is_google_not_found(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 404 or getattr(getattr(exc, "resp", None), "status", None) == 404


def _create_google_event(service, calendar_id: str, shift: PatchworkShiftData) -> str:
    created = service.events().insert(calendarId=calendar_id, body=_google_event_body(shift)).execute()
    google_event_id = created.get("id", "")
    if not google_event_id:
        raise RuntimeError("Google Calendar did not return an event id for a Patchwork shift.")
    return google_event_id


def _delete_google_event(service, record: PatchworkShift) -> None:
    if not record.google_event_id or not record.calendar_id:
        return
    try:
        service.events().delete(calendarId=record.calendar_id, eventId=record.google_event_id).execute()
    except Exception as exc:  # A manually deleted mirror is already in the desired state.
        if not _is_google_not_found(exc):
            raise


def sync_patchwork_calendar(*, fetcher=fetch_patchwork_calendar, calendar_service=None, now=None) -> SyncResult:
    """Mirror the current Patchwork feed into the configured shared calendar.

    It is safe to run repeatedly. Only a new/changed source UID results in a
    Google write, while removed/cancelled shifts are deleted from the mirror.
    """
    calendar_id = settings.GOOGLE_CALENDAR_ID
    if not calendar_id:
        raise RuntimeError("GOOGLE_CALENDAR_ID is not configured.")

    shifts = parse_patchwork_calendar(fetcher())
    service = calendar_service or build(
        "calendar", "v3", credentials=get_google_credentials(), cache_discovery=False
    )
    now = now or timezone.now()
    existing = {record.source_uid: record for record in PatchworkShift.objects.all()}
    seen_uids = {shift.uid for shift in shifts}
    created = updated = deactivated = unchanged = 0

    for shift in shifts:
        record = existing.get(shift.uid)
        cancelled = shift.status == "CANCELLED"
        if cancelled:
            if record and record.active:
                _delete_google_event(service, record)
                record.active = False
                record.source_status = shift.status
                record.last_seen_at = now
                record.payload_hash = shift.payload_hash
                record.save(update_fields=["active", "source_status", "last_seen_at", "payload_hash", "updated_at"])
                deactivated += 1
            continue

        if record is None:
            google_event_id = _create_google_event(service, calendar_id, shift)
            PatchworkShift.objects.create(
                source_uid=shift.uid,
                google_event_id=google_event_id,
                calendar_id=calendar_id,
                starts_at=shift.starts_at,
                ends_at=shift.ends_at,
                timezone=shift.timezone_name,
                all_day=shift.all_day,
                source_status=shift.status,
                payload_hash=shift.payload_hash,
                last_seen_at=now,
            )
            created += 1
            continue

        needs_update = (
            not record.active
            or record.payload_hash != shift.payload_hash
            or record.calendar_id != calendar_id
            or not record.google_event_id
        )
        if needs_update:
            try:
                if record.google_event_id and record.calendar_id == calendar_id:
                    service.events().patch(
                        calendarId=calendar_id, eventId=record.google_event_id, body=_google_event_body(shift)
                    ).execute()
                    google_event_id = record.google_event_id
                else:
                    google_event_id = _create_google_event(service, calendar_id, shift)
            except Exception as exc:
                if not _is_google_not_found(exc):
                    raise
                google_event_id = _create_google_event(service, calendar_id, shift)

            record.google_event_id = google_event_id
            record.calendar_id = calendar_id
            record.starts_at = shift.starts_at
            record.ends_at = shift.ends_at
            record.timezone = shift.timezone_name
            record.all_day = shift.all_day
            record.source_status = shift.status
            record.payload_hash = shift.payload_hash
            record.active = True
            record.last_seen_at = now
            record.save()
            updated += 1
        else:
            record.last_seen_at = now
            record.save(update_fields=["last_seen_at", "updated_at"])
            unchanged += 1

    for record in existing.values():
        if record.active and record.source_uid not in seen_uids:
            _delete_google_event(service, record)
            record.active = False
            record.last_seen_at = now
            record.save(update_fields=["active", "last_seen_at", "updated_at"])
            deactivated += 1

    return SyncResult(created=created, updated=updated, deactivated=deactivated, unchanged=unchanged)
