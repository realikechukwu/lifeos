"""Unit tests for the deterministic RRULE builder (Python, not the model)
and its interaction with the duplicate-detection key. Mirrors the "never
block a post" philosophy already covered by test_calendar_event_defaults.py
— an unrecognised or internally inconsistent recurrence combination must
fall back to a one-off event, never raise, never block."""

from datetime import date

from django.test import SimpleTestCase

from assistant.services.extractor import build_recurrence
from assistant.services.google_calendar import compute_duplicate_key


class BuildRecurrenceTests(SimpleTestCase):
    def test_no_frequency_means_no_recurrence(self):
        result = build_recurrence(None, None, None, None, None, date(2026, 8, 18), False)
        self.assertIsNone(result.rrule)
        self.assertIsNone(result.description)
        self.assertIsNone(result.note)

    def test_unrecognised_frequency_falls_back_to_one_off(self):
        result = build_recurrence("fortnightly", None, None, None, None, date(2026, 8, 18), False)
        self.assertIsNone(result.rrule)

    def test_simple_weekly(self):
        # 2026-08-18 is a Tuesday.
        result = build_recurrence("weekly", None, None, None, None, date(2026, 8, 18), False)
        self.assertEqual(result.rrule, "RRULE:FREQ=WEEKLY")
        self.assertEqual(result.description, "Repeats weekly")
        self.assertIsNone(result.note)

    def test_weekly_with_named_days(self):
        result = build_recurrence("weekly", None, ["MO", "WE", "FR"], None, None, date(2026, 8, 17), False)
        self.assertEqual(result.rrule, "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR")
        self.assertIn("Monday, Wednesday, Friday", result.description)

    def test_interval_is_included_when_not_one(self):
        result = build_recurrence("weekly", 2, None, None, None, date(2026, 8, 18), False)
        self.assertEqual(result.rrule, "RRULE:FREQ=WEEKLY;INTERVAL=2")
        self.assertEqual(result.description, "Repeats every 2 weeks")

    def test_interval_of_one_is_omitted(self):
        result = build_recurrence("daily", 1, None, None, None, date(2026, 8, 18), False)
        self.assertEqual(result.rrule, "RRULE:FREQ=DAILY")

    def test_non_positive_interval_silently_defaults_to_one_without_a_note(self):
        result = build_recurrence("weekly", 0, None, None, None, date(2026, 8, 18), False)
        self.assertEqual(result.rrule, "RRULE:FREQ=WEEKLY")
        self.assertIsNone(result.note)

    def test_byday_is_dropped_for_monthly(self):
        result = build_recurrence("monthly", None, ["MO"], None, None, date(2026, 8, 18), False)
        self.assertEqual(result.rrule, "RRULE:FREQ=MONTHLY")
        self.assertNotIn("BYDAY", result.rrule)

    def test_byday_is_dropped_for_yearly(self):
        result = build_recurrence("yearly", None, ["MO"], None, None, date(2026, 8, 18), False)
        self.assertEqual(result.rrule, "RRULE:FREQ=YEARLY")

    def test_count_is_included(self):
        result = build_recurrence("weekly", None, None, None, 8, date(2026, 8, 18), False)
        self.assertEqual(result.rrule, "RRULE:FREQ=WEEKLY;COUNT=8")
        self.assertEqual(result.description, "Repeats weekly, 8 times")

    def test_invalid_count_is_dropped(self):
        result = build_recurrence("weekly", None, None, None, 0, date(2026, 8, 18), False)
        self.assertEqual(result.rrule, "RRULE:FREQ=WEEKLY")

    def test_until_for_all_day_event_uses_date_value(self):
        result = build_recurrence(
            "weekly", None, None, date(2026, 12, 15), None, date(2026, 8, 18), True
        )
        self.assertEqual(result.rrule, "RRULE:FREQ=WEEKLY;UNTIL=20261215")
        self.assertIn("until 15 Dec 2026", result.description)

    def test_until_for_timed_event_uses_utc_datetime_value(self):
        result = build_recurrence(
            "weekly", None, None, date(2026, 12, 15), None, date(2026, 8, 18), False
        )
        self.assertIn("UNTIL=20261215T", result.rrule)
        self.assertTrue(result.rrule.endswith("Z"))

    def test_until_before_start_is_dropped_with_a_note(self):
        result = build_recurrence(
            "weekly", None, None, date(2026, 1, 1), None, date(2026, 8, 18), False
        )
        self.assertEqual(result.rrule, "RRULE:FREQ=WEEKLY")
        self.assertIsNotNone(result.note)
        self.assertIn("ignored", result.note)

    def test_until_and_count_both_given_prefers_until(self):
        result = build_recurrence(
            "weekly", None, None, date(2026, 12, 15), 8, date(2026, 8, 18), True
        )
        self.assertIn("UNTIL=20261215", result.rrule)
        self.assertNotIn("COUNT", result.rrule)
        self.assertIsNotNone(result.note)
        self.assertIn("count was ignored", result.note)

    def test_unrecognised_weekday_codes_are_dropped(self):
        result = build_recurrence("weekly", None, ["Mon", "XX"], None, None, date(2026, 8, 18), False)
        self.assertEqual(result.rrule, "RRULE:FREQ=WEEKLY")


class ComputeDuplicateKeyRecurrenceTests(SimpleTestCase):
    def test_one_off_and_recurring_event_hash_differently(self):
        one_off_key = compute_duplicate_key("Team standup", date(2026, 8, 18), "09:00", "primary")
        recurring_key = compute_duplicate_key(
            "Team standup", date(2026, 8, 18), "09:00", "primary", recurrence_frequency="weekly"
        )
        self.assertNotEqual(one_off_key, recurring_key)

    def test_key_is_deterministic_for_the_same_recurrence(self):
        key1 = compute_duplicate_key(
            "Team standup", date(2026, 8, 18), "09:00", "primary", recurrence_frequency="weekly"
        )
        key2 = compute_duplicate_key(
            "Team standup", date(2026, 8, 18), "09:00", "primary", recurrence_frequency="weekly"
        )
        self.assertEqual(key1, key2)
