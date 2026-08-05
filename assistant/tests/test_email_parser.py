from pathlib import Path

from django.test import SimpleTestCase

from assistant.services.email_parser import split_new_instruction_and_forward, extract_forwarded_date, extract_forwarded_sender

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


class ForwardedMessageParsingTests(SimpleTestCase):
    def test_forwarded_message_splits_instruction_from_forwarded_body_and_finds_date(self):
        text = load_fixture("forwarded_relative_date.txt")
        new_instruction, forwarded_body, forwarded_headers = split_new_instruction_and_forward(text)

        self.assertIn("add this", new_instruction.lower())
        self.assertIn("check-up", forwarded_body.lower())

        forwarded_date = extract_forwarded_date(forwarded_headers)
        self.assertIsNotNone(forwarded_date)
        self.assertEqual(forwarded_date.year, 2026)
        self.assertEqual(forwarded_date.month, 7)
        self.assertEqual(forwarded_date.day, 31)

        forwarded_sender = extract_forwarded_sender(forwarded_headers)
        self.assertEqual(forwarded_sender, "appointments@riversidevets.example.com")

    def test_plain_email_with_no_forward_marker_has_no_forwarded_body(self):
        new_instruction, forwarded_body, forwarded_headers = split_new_instruction_and_forward(
            "Just a plain note with no forwarded content."
        )
        self.assertEqual(forwarded_body, "")
        self.assertEqual(forwarded_headers, "")
        self.assertIn("plain note", new_instruction)
