from datetime import date, time

from django.test import SimpleTestCase

from assistant.services.extractor import (
    combine_date_and_time,
    parse_24h_time,
    parse_iso_date,
    times_are_valid,
)


class DateTimeConversionTests(SimpleTestCase):
    def test_parse_iso_date(self):
        self.assertEqual(parse_iso_date("2026-08-18"), date(2026, 8, 18))
        self.assertIsNone(parse_iso_date("18/08/2026"))
        self.assertIsNone(parse_iso_date(None))

    def test_parse_24h_time(self):
        self.assertEqual(parse_24h_time("10:30"), time(10, 30))
        self.assertIsNone(parse_24h_time("10:30am"))
        self.assertIsNone(parse_24h_time(None))

    def test_combine_date_and_time_produces_aware_datetime(self):
        dt = combine_date_and_time(date(2026, 8, 18), time(10, 30))
        self.assertIsNotNone(dt.tzinfo)  # never naive

    def test_summer_date_is_british_summer_time_utc_plus_one(self):
        # 18 August is BST (UTC+1).
        dt = combine_date_and_time(date(2026, 8, 18), time(10, 30))
        offset = dt.utcoffset()
        self.assertEqual(offset.total_seconds(), 3600)

    def test_winter_date_is_gmt_utc(self):
        # 18 January is GMT (UTC+0).
        dt = combine_date_and_time(date(2026, 1, 18), time(10, 30))
        offset = dt.utcoffset()
        self.assertEqual(offset.total_seconds(), 0)

    def test_end_time_after_start_time_is_valid(self):
        self.assertTrue(times_are_valid(date(2026, 8, 18), time(10, 0), time(11, 0)))

    def test_end_time_before_start_time_is_invalid(self):
        self.assertFalse(times_are_valid(date(2026, 8, 18), time(11, 0), time(10, 0)))

    def test_missing_end_time_is_treated_as_valid(self):
        self.assertTrue(times_are_valid(date(2026, 8, 18), time(10, 0), None))
