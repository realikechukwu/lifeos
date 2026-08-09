"""One-way Patchwork iCalendar synchronisation for Ike's work shifts.

The Patchwork subscription URL is a private bearer credential and is never
logged. Event summaries are retained as shift labels, while descriptions and
other source details remain excluded from the household calendar.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone
from googleapiclient.discovery import build

from core.models import PatchworkShift

from .gmail import get_google_credentials


WORK_SHIFT_FALLBACK_TITLE = "Ike work shift"
WORK_SHIFT_DESCRIPTION = "Synced automatically from Patchwork. Edit this shift in Patchwork."
TIME_OFF_LABEL_MARKERS = ("leave", "time off")


@dataclass(frozen=True)
class PatchworkShiftData:
    uid: str
    label: str
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
    suppressed: int = 0


def fetch_patchwork_calendar() -> bytes:
    """Fetch the iCalendar bytes without exposing the subscription URL."""
    url = settings.PATCHWORK_CALENDAR_URL.strip()
    if not url:
        raise RuntimeError("PATCHWORK_CALENDAR_URL is not configured.")
    # Patchwork's edge blocks Python's default urllib user agent (HTTP 403),
    # while accepting normal calendar-feed clients. Keep this explicit so a
    # dependency upgrade cannot silently restore the rejected default.
    request = Request(url, headers={"Accept": "text/calendar", "User-Agent": "curl/8.7.1"})
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


def _normalise_ical_text(value: str) -> str:
    """Decode the RFC 5545 escapes used by Patchwork's SUMMARY values."""
    decoded: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            escaped = value[index + 1]
            decoded.append(" " if escaped in ("n", "N") else escaped)
            index += 2
            continue
        decoded.append(value[index])
        index += 1
    return re.sub(r"\s+", " ", "".join(decoded)).strip()[:255]


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


def _hash_shift(
    *, label: str, starts_at: datetime, ends_at: datetime | None, timezone_name: str, all_day: bool, status: str
) -> str:
    values = (label, starts_at.isoformat(), ends_at.isoformat() if ends_at else "", timezone_name, str(all_day), status)
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
        label = _normalise_ical_text(event.get("SUMMARY", ({}, ""))[1])
        shifts.append(PatchworkShiftData(
            uid=uid,
            label=label,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone_name=timezone_name,
            all_day=all_day,
            status=status,
            payload_hash=_hash_shift(
                label=label,
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


def _shift_title(label: str) -> str:
    return f"Ike — {label}" if label else WORK_SHIFT_FALLBACK_TITLE


def _is_time_off(shift: PatchworkShiftData) -> bool:
    label = shift.label.casefold()
    return any(marker in label for marker in TIME_OFF_LABEL_MARKERS)


def _local_start_date(shift: PatchworkShiftData) -> date:
    return shift.starts_at.astimezone(_zone(shift.timezone_name)).date()


def _suppressed_work_shift_uids(shifts: list[PatchworkShiftData]) -> set[str]:
    """Find redundant work intervals without hiding leave or adjacent shifts.

    Patchwork commonly emits 09:00–13:00 and 13:00–17:00 component events
    alongside a covering 09:00–17:00 event. A component is suppressed only
    when another work event on the same local start date fully contains it.
    Exact duplicate intervals keep the first feed entry. Time-off/leave items
    are excluded so a leave annotation is never hidden by a scheduled shift.
    """
    suppressed: set[str] = set()
    candidates = [
        (index, shift)
        for index, shift in enumerate(shifts)
        if shift.status != "CANCELLED" and shift.ends_at is not None and not _is_time_off(shift)
    ]

    for index, shift in candidates:
        for other_index, other in candidates:
            if shift.uid == other.uid or _local_start_date(shift) != _local_start_date(other):
                continue
            contains = other.starts_at <= shift.starts_at and other.ends_at >= shift.ends_at
            strictly_larger = other.starts_at < shift.starts_at or other.ends_at > shift.ends_at
            earlier_exact_duplicate = (
                other.starts_at == shift.starts_at and other.ends_at == shift.ends_at and other_index < index
            )
            if contains and (strictly_larger or earlier_exact_duplicate):
                suppressed.add(shift.uid)
                break
    return suppressed


def _google_event_body(shift: PatchworkShiftData) -> dict:
    body = {
        "summary": _shift_title(shift.label),
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


def _update_record_from_shift(
    record: PatchworkShift, shift: PatchworkShiftData, *, now: datetime, active: bool, suppressed: bool
) -> None:
    record.starts_at = shift.starts_at
    record.ends_at = shift.ends_at
    record.timezone = shift.timezone_name
    record.all_day = shift.all_day
    record.source_label = shift.label
    record.source_status = shift.status
    record.payload_hash = shift.payload_hash
    record.active = active
    record.suppressed = suppressed
    record.last_seen_at = now


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
    suppressed_uids = _suppressed_work_shift_uids(shifts)
    created = updated = deactivated = unchanged = suppressed_count = 0

    for shift in shifts:
        record = existing.get(shift.uid)
        cancelled = shift.status == "CANCELLED"
        if cancelled:
            if record:
                if record.active:
                    _delete_google_event(service, record)
                    deactivated += 1
                _update_record_from_shift(record, shift, now=now, active=False, suppressed=False)
                record.save()
            continue

        if shift.uid in suppressed_uids:
            suppressed_count += 1
            if record is None:
                PatchworkShift.objects.create(
                    source_uid=shift.uid,
                    calendar_id=calendar_id,
                    starts_at=shift.starts_at,
                    ends_at=shift.ends_at,
                    timezone=shift.timezone_name,
                    all_day=shift.all_day,
                    source_label=shift.label,
                    source_status=shift.status,
                    payload_hash=shift.payload_hash,
                    active=False,
                    suppressed=True,
                    last_seen_at=now,
                )
            else:
                if record.active:
                    _delete_google_event(service, record)
                    deactivated += 1
                _update_record_from_shift(record, shift, now=now, active=False, suppressed=True)
                record.save()
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
                source_label=shift.label,
                source_status=shift.status,
                payload_hash=shift.payload_hash,
                suppressed=False,
                last_seen_at=now,
            )
            created += 1
            continue

        needs_update = (
            not record.active
            or record.suppressed
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
            _update_record_from_shift(record, shift, now=now, active=True, suppressed=False)
            record.save()
            updated += 1
        else:
            record.last_seen_at = now
            record.save(update_fields=["last_seen_at", "updated_at"])
            unchanged += 1

    for record in existing.values():
        if record.source_uid not in seen_uids and (record.active or record.suppressed):
            if record.active:
                _delete_google_event(service, record)
                deactivated += 1
            record.active = False
            record.suppressed = False
            record.last_seen_at = now
            record.save(update_fields=["active", "suppressed", "last_seen_at", "updated_at"])

    return SyncResult(
        created=created,
        updated=updated,
        deactivated=deactivated,
        unchanged=unchanged,
        suppressed=suppressed_count,
    )
