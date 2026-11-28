"""Colour-coded calendars: each event goes on its category's calendar.

Apple Calendar colours events by calendar, so LifeOS keeps work, family and
health events on their own calendars and everything else on the main one.
"""

from datetime import date, time, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from assistant.agent.proposals import tool_definitions, validate_payload
from assistant.services.calendar_categories import (
    calendar_id_for, category_for_calendar_id, guess_category, managed_calendar_ids, resolve_category,
)
from assistant.services.extractor import AssistantAction
from assistant.services.google_calendar import create_event_for_parsed_action, list_calendar_events
from assistant.services.patchwork_calendar import _google_event_body, parse_patchwork_calendar, sync_patchwork_calendar
from assistant.services.router import build_parsed_action_from_extraction
from core.models import CalendarEventRecord, IncomingEmail, ParsedAction, PatchworkShift

from .test_patchwork_calendar import calendar_with_event

CATEGORY_CALENDARS = {
    "GOOGLE_CALENDAR_ID": "main-cal",
    "GOOGLE_CALENDAR_ID_WORK": "work-cal",
    "GOOGLE_CALENDAR_ID_FAMILY": "family-cal",
    "GOOGLE_CALENDAR_ID_HEALTH": "health-cal",
}


class CategoryRoutingTests(TestCase):
    @override_settings(GOOGLE_CALENDAR_ID="main-cal")
    def test_unset_category_calendars_fall_back_to_the_main_calendar(self):
        for category in ("work", "family", "health", "personal", "", None):
            self.assertEqual(calendar_id_for(category), "main-cal")
        self.assertEqual(managed_calendar_ids(), ["main-cal"])

    @override_settings(**CATEGORY_CALENDARS)
    def test_each_category_has_its_own_calendar(self):
        self.assertEqual(calendar_id_for("work"), "work-cal")
        self.assertEqual(calendar_id_for("health"), "health-cal")
        self.assertEqual(calendar_id_for("personal"), "main-cal")
        self.assertEqual(managed_calendar_ids(), ["main-cal", "work-cal", "family-cal", "health-cal"])
        self.assertEqual(category_for_calendar_id("family-cal"), "family")
        self.assertEqual(category_for_calendar_id("main-cal"), "personal")

    @override_settings(GOOGLE_CALENDAR_ID="")
    def test_missing_main_calendar_is_an_error(self):
        with self.assertRaisesMessage(RuntimeError, "GOOGLE_CALENDAR_ID is not configured."):
            calendar_id_for("work")

    def test_keyword_rules(self):
        cases = {
            "Ike — Night — General Medicine": "work",
            "On-call weekend": "work",
            "GP appointment": "health",
            "Dentist for the kids": "health",
            "Nursery parents' evening": "family",
            "Family day out at the zoo": "family",
            "Mum's birthday": None,
            "Pay council tax": None,
            "Date night": None,
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(guess_category(title), expected)

    def test_ai_choice_wins_over_keywords_and_unknown_is_personal(self):
        self.assertEqual(resolve_category("family", "GP appointment"), "family")
        self.assertEqual(resolve_category("", "GP appointment"), "health")
        self.assertEqual(resolve_category(None, "Coffee with Sam"), "personal")


@override_settings(AUTHORISED_EMAIL_IKE="ike@example.com", **CATEGORY_CALENDARS)
@patch("assistant.services.google_calendar.get_google_credentials")
@patch("assistant.services.google_calendar.build")
class CreateEventCategoryTests(TestCase):
    def setUp(self):
        self.email = IncomingEmail.objects.create(
            gmail_message_id="msg-cat", outer_sender="ike@example.com", status=IncomingEmail.Status.RECEIVED
        )
        self.day = timezone.localdate() + timedelta(days=5)

    def _action(self, title, category="", **extra):
        return ParsedAction.objects.create(
            incoming_email=self.email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title=title,
            appointment_date=self.day,
            start_time=time(11, 0),
            calendar_category=category,
            confidence=1.0,
            **extra,
        )

    def _service(self, mock_build, event_id="evt-1"):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": event_id}
        mock_build.return_value = service
        return service

    def test_event_goes_on_its_category_calendar(self, mock_build, _creds):
        service = self._service(mock_build)

        record = create_event_for_parsed_action(self._action("Dentist", "health"))

        self.assertEqual(service.events.return_value.insert.call_args.kwargs["calendarId"], "health-cal")
        self.assertEqual(record.calendar_id, "health-cal")

    def test_keyword_rules_place_an_event_without_a_category_and_remember_it(self, mock_build, _creds):
        service = self._service(mock_build)
        action = self._action("Nursery sports day")

        record = create_event_for_parsed_action(action)

        self.assertEqual(service.events.return_value.insert.call_args.kwargs["calendarId"], "family-cal")
        self.assertEqual(record.calendar_id, "family-cal")
        action.refresh_from_db()
        self.assertEqual(action.calendar_category, "family")

    def test_uncategorised_event_stays_on_the_main_calendar(self, mock_build, _creds):
        service = self._service(mock_build)

        create_event_for_parsed_action(self._action("Coffee with Sam"))

        self.assertEqual(service.events.return_value.insert.call_args.kwargs["calendarId"], "main-cal")

    def test_repeat_sorted_into_a_different_category_is_still_a_duplicate(self, mock_build, _creds):
        service = self._service(mock_build)
        first = create_event_for_parsed_action(self._action("Check-up", "health"))

        again = self._action("Check-up", "personal")
        result = create_event_for_parsed_action(again)

        self.assertEqual(result, first)
        service.events.return_value.insert.assert_called_once()
        again.refresh_from_db()
        self.assertEqual(again.status, ParsedAction.Status.REJECTED)

    def test_booking_reference_is_found_on_any_category_calendar(self, mock_build, _creds):
        self._service(mock_build)
        create_event_for_parsed_action(self._action("Hospital scan", "health", booking_reference="REF-9"))

        moved = self._action("Hospital scan", "personal", booking_reference="REF-9")
        moved.appointment_date = self.day + timedelta(days=1)
        moved.save()

        self.assertIsNone(create_event_for_parsed_action(moved))
        moved.refresh_from_db()
        self.assertEqual(moved.status, ParsedAction.Status.PENDING_REVIEW)


@override_settings(**CATEGORY_CALENDARS)
class ListCalendarEventsTests(TestCase):
    @patch("assistant.services.google_calendar.get_google_credentials")
    @patch("assistant.services.google_calendar.build")
    def test_reads_every_calendar_and_merges_in_time_order(self, mock_build, _creds):
        items = {
            "main-cal": [{"id": "b", "summary": "Coffee", "start": {"dateTime": "2026-12-01T15:00:00Z"}}],
            "work-cal": [{"id": "a", "summary": "Ike — Long Day", "start": {"dateTime": "2026-12-01T08:00:00Z"}}],
            "family-cal": [{"id": "c", "summary": "Zoo", "start": {"date": "2026-12-01"}}],
            "health-cal": [],
        }

        def fake_list(**params):
            request = MagicMock()
            request.execute.return_value = {"items": items[params["calendarId"]]}
            return request

        mock_build.return_value.events.return_value.list.side_effect = fake_list

        events = list_calendar_events(date(2026, 12, 1), date(2026, 12, 2))

        self.assertEqual([e["title"] for e in events], ["Zoo", "Ike — Long Day", "Coffee"])
        self.assertEqual([e["category"] for e in events], ["family", "work", "personal"])


class ExtractionAndAgentTests(TestCase):
    def test_router_keeps_the_category_for_events_only(self):
        email = IncomingEmail.objects.create(gmail_message_id="msg-router", outer_sender="ike@example.com")
        event = build_parsed_action_from_extraction(email, AssistantAction(
            action_type="create_calendar_event", title="GP", calendar_category="health", confidence=1.0,
        ))
        task = build_parsed_action_from_extraction(email, AssistantAction(
            action_type="create_task", title="Buy nappies", calendar_category="family", confidence=1.0,
        ))
        self.assertEqual(event.calendar_category, "health")
        self.assertEqual(task.calendar_category, "")

    def test_agent_event_tool_offers_the_categories_with_guidance(self):
        propose_event = next(d for d in tool_definitions() if d["name"] == "propose_event")
        prop = propose_event["input_schema"]["properties"]["calendar_category"]
        self.assertEqual(prop["enum"], ["work", "family", "health", "personal", None])
        self.assertIn("nursery", prop["description"])

    def test_agent_event_proposal_accepts_a_category(self):
        day = (timezone.localdate() + timedelta(days=3)).isoformat()
        payload = validate_payload("create_event", {
            "title": "Dentist", "appointment_date": day, "start_time": "10:00", "calendar_category": "health",
        })
        self.assertEqual(payload["calendar_category"], "health")


@override_settings(GOOGLE_CALENDAR_ID="shared-calendar", APP_TIMEZONE="Europe/London")
class PatchworkWorkCalendarTests(TestCase):
    def setUp(self):
        self.service = MagicMock()
        self.service.events.return_value.insert.return_value.execute.return_value = {"id": "google-shift-1"}
        self.live_event = {"id": "google-shift-1", **_google_event_body(parse_patchwork_calendar(calendar_with_event())[0])}
        self.service.events.return_value.list.return_value.execute.return_value = {"items": [self.live_event]}

    def test_shifts_go_on_the_work_calendar(self):
        with self.settings(GOOGLE_CALENDAR_ID_WORK="work-cal"):
            sync_patchwork_calendar(fetcher=lambda: calendar_with_event(), calendar_service=self.service)
        self.assertEqual(self.service.events.return_value.insert.call_args.kwargs["calendarId"], "work-cal")
        self.assertEqual(PatchworkShift.objects.get().calendar_id, "work-cal")

    def test_existing_shift_is_moved_to_the_work_calendar_not_copied(self):
        sync_patchwork_calendar(fetcher=lambda: calendar_with_event(), calendar_service=self.service)
        self.assertEqual(PatchworkShift.objects.get().calendar_id, "shared-calendar")
        self.service.reset_mock()
        self.service.events.return_value.list.return_value.execute.return_value = {"items": []}
        self.service.events.return_value.move.return_value.execute.return_value = dict(self.live_event)

        with self.settings(GOOGLE_CALENDAR_ID_WORK="work-cal"):
            result = sync_patchwork_calendar(fetcher=lambda: calendar_with_event(), calendar_service=self.service)

        self.service.events.return_value.move.assert_called_once_with(
            calendarId="shared-calendar", eventId="google-shift-1", destination="work-cal"
        )
        self.service.events.return_value.insert.assert_not_called()
        self.assertEqual(result.unchanged, 1)
        shift = PatchworkShift.objects.get()
        self.assertEqual((shift.calendar_id, shift.google_event_id), ("work-cal", "google-shift-1"))

    def test_shift_missing_from_the_old_calendar_is_recreated_on_work(self):
        sync_patchwork_calendar(fetcher=lambda: calendar_with_event(), calendar_service=self.service)
        self.service.reset_mock()
        self.service.events.return_value.list.return_value.execute.return_value = {"items": []}
        gone = RuntimeError("Not found")
        gone.resp = SimpleNamespace(status=404)
        self.service.events.return_value.move.return_value.execute.side_effect = gone
        self.service.events.return_value.insert.return_value.execute.return_value = {"id": "replacement"}

        with self.settings(GOOGLE_CALENDAR_ID_WORK="work-cal"):
            sync_patchwork_calendar(fetcher=lambda: calendar_with_event(), calendar_service=self.service)

        self.assertEqual(self.service.events.return_value.insert.call_args.kwargs["calendarId"], "work-cal")
        shift = PatchworkShift.objects.get()
        self.assertEqual((shift.calendar_id, shift.google_event_id), ("work-cal", "replacement"))


@override_settings(**CATEGORY_CALENDARS)
@patch("assistant.management.commands.sort_calendar_events.get_google_credentials")
@patch("assistant.management.commands.sort_calendar_events.build")
class SortCalendarEventsCommandTests(TestCase):
    def setUp(self):
        email = IncomingEmail.objects.create(gmail_message_id="msg-sort", outer_sender="ike@example.com")
        action = ParsedAction.objects.create(
            incoming_email=email, action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT, title="Swim",
        )
        CalendarEventRecord.objects.create(
            parsed_action=action, incoming_email=email, google_event_id="lifeos-1", calendar_id="main-cal",
            title="Swim", appointment_date=date(2026, 12, 3),
        )
        PatchworkShift.objects.create(
            source_uid="uid-1", google_event_id="shift-1", calendar_id="main-cal",
            starts_at=timezone.now(), source_label="Night — General Medicine",
        )
        self.items = [
            {"id": "shift-1", "summary": "Ike — Night — General Medicine", "start": {"dateTime": "2026-11-13T00:00:00Z"}},
            {"id": "gp-1", "summary": "GP appointment", "start": {"dateTime": "2026-11-13T11:00:00Z"}},
            {"id": "lifeos-1", "summary": "Swim", "start": {"date": "2026-12-03"}},
            {"id": "coffee-1", "summary": "Coffee with Sam", "start": {"date": "2026-12-04"}},
            {"id": "invite-1", "summary": "Team dinner", "organizer": {"self": False}, "start": {"date": "2026-12-05"}},
            {"id": "gp-1_20261120", "recurringEventId": "gp-1", "summary": "GP appointment", "start": {"date": "2026-11-20"}},
        ]

    def _run(self, mock_build, *args, ai=("family", "personal")):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {"items": self.items}
        mock_build.return_value = service
        out = StringIO()
        with patch("assistant.management.commands.sort_calendar_events.classify_with_ai", return_value=list(ai)) as ai_mock:
            call_command("sort_calendar_events", *args, stdout=out)
        return service, out.getvalue(), ai_mock

    def test_trial_run_moves_nothing_and_shows_the_plan(self, mock_build, _creds):
        service, output, ai_mock = self._run(mock_build)

        service.events.return_value.move.assert_not_called()
        ai_mock.assert_called_once_with([
            {"title": "Swim", "location": ""}, {"title": "Coffee with Sam", "location": ""},
        ])
        self.assertIn("WORK (1, moves)", output)
        self.assertIn("Ike — Night — General Medicine  [shift]", output)
        self.assertIn("GP appointment  [keywords]", output)
        self.assertIn("PERSONAL (1, stays on the main calendar)", output)
        self.assertIn("Team dinner  (an invitation from someone else", output)
        self.assertIn("Trial run: nothing was moved", output)
        self.assertEqual(CalendarEventRecord.objects.get().calendar_id, "main-cal")

    def test_apply_moves_events_and_updates_lifeos_records(self, mock_build, _creds):
        service, output, _ = self._run(mock_build, "--apply")

        moves = {
            call.kwargs["eventId"]: call.kwargs["destination"]
            for call in service.events.return_value.move.call_args_list
        }
        self.assertEqual(moves, {"shift-1": "work-cal", "gp-1": "health-cal", "lifeos-1": "family-cal"})
        self.assertEqual(CalendarEventRecord.objects.get().calendar_id, "family-cal")
        self.assertEqual(ParsedAction.objects.get().calendar_category, "family")
        self.assertEqual(PatchworkShift.objects.get().calendar_id, "work-cal")
        self.assertIn("Moved 1 to work, 1 to family, 1 to health.", output)

    def test_set_overrides_a_category_and_no_ai_leaves_the_rest_personal(self, mock_build, _creds):
        service, _, ai_mock = self._run(mock_build, "--apply", "--no-ai", "--set", "coffee-1=family")

        ai_mock.assert_not_called()
        moves = {
            call.kwargs["eventId"]: call.kwargs["destination"]
            for call in service.events.return_value.move.call_args_list
        }
        self.assertEqual(moves, {"shift-1": "work-cal", "gp-1": "health-cal", "coffee-1": "family-cal"})

    def test_refuses_to_run_before_category_calendars_exist(self, mock_build, _creds):
        with self.settings(GOOGLE_CALENDAR_ID_WORK="", GOOGLE_CALENDAR_ID_FAMILY="", GOOGLE_CALENDAR_ID_HEALTH=""):
            with self.assertRaisesMessage(CommandError, "No category calendars are set"):
                call_command("sort_calendar_events", stdout=StringIO())
