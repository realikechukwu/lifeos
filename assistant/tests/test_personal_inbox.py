import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from assistant.agent.tools import HANDLERS, TOOL_DEFINITIONS, ToolContext, ToolInputError
from assistant.services import personal_inbox
from core.models import GoogleCredential, TelegramChat

INBOX_SETTINGS = {
    "AGENT_PERSONAL_INBOX_ENABLED": True,
    "PERSONAL_GMAIL_IKE": "Ike.Personal@example.com",
    "PERSONAL_GMAIL_WIFE": "",
}


def _ctx(role="ike", chat_type="private"):
    chat = TelegramChat(chat_id=1, chat_type=chat_type, authorised=True)
    return ToolContext(role=role, speaker_name="Ike" if role == "ike" else "Ike's wife", telegram_chat=chat)


@override_settings(**INBOX_SETTINGS)
class PersonalInboxToolTests(TestCase):
    @patch("assistant.agent.tools.personal_inbox.search_messages")
    def test_search_uses_the_speakers_own_address(self, search):
        search.return_value = [{"message_id": "abc123", "subject": "Parents evening"}]
        result = json.loads(HANDLERS["search_my_inbox"](_ctx(), {"query": "from:school", "max_results": 50}))

        search.assert_called_once_with("ike.personal@example.com", "from:school", max_results=25)
        self.assertEqual(result["messages"][0]["subject"], "Parents evening")
        self.assertIn("untrusted", result["note"])

    @patch("assistant.agent.tools.personal_inbox.read_message")
    def test_read_returns_the_message(self, read):
        read.return_value = {"message_id": "abc123", "body": "See you Thursday"}
        result = json.loads(HANDLERS["read_my_email"](_ctx(), {"message_id": "abc123"}))
        read.assert_called_once_with("ike.personal@example.com", "abc123")
        self.assertEqual(result["body"], "See you Thursday")

    def test_group_chat_is_refused(self):
        with self.assertRaisesMessage(ToolInputError, "private chat"):
            HANDLERS["search_my_inbox"](_ctx(chat_type="group"), {"query": "", "max_results": 5})

    def test_person_without_a_connected_inbox_is_refused(self):
        with self.assertRaisesMessage(ToolInputError, "has not connected"):
            HANDLERS["search_my_inbox"](_ctx(role="wife"), {"query": "", "max_results": 5})

    @override_settings(AGENT_PERSONAL_INBOX_ENABLED=False)
    def test_switched_off_is_refused(self):
        with self.assertRaisesMessage(ToolInputError, "switched off"):
            HANDLERS["read_my_email"](_ctx(), {"message_id": "abc123"})

    def test_message_id_must_be_a_gmail_id(self):
        with self.assertRaises(ToolInputError):
            HANDLERS["read_my_email"](_ctx(), {"message_id": "../other"})

    def test_tools_are_declared_in_the_stable_list(self):
        names = [tool["name"] for tool in TOOL_DEFINITIONS]
        self.assertIn("search_my_inbox", names)
        self.assertIn("read_my_email", names)
        self.assertEqual(names, sorted(names))


class PersonalInboxServiceTests(TestCase):
    def _service(self):
        service = MagicMock()
        messages = service.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
        return service, messages

    def test_search_returns_metadata(self):
        service, messages = self._service()
        messages.get.return_value.execute.return_value = {
            "id": "m1", "snippet": "Reminder about…", "labelIds": ["INBOX", "UNREAD"],
            "payload": {"headers": [
                {"name": "From", "value": "School <office@school.example>"},
                {"name": "Subject", "value": "Parents evening"},
                {"name": "Date", "value": "Wed, 14 Oct 2026 09:00:00 +0100"},
            ]},
        }
        with patch.object(personal_inbox, "_service", return_value=service):
            results = personal_inbox.search_messages("ike@example.com", "from:school", max_results=5)

        messages.list.assert_called_once_with(userId="me", q="from:school", maxResults=5)
        self.assertEqual(results[0]["subject"], "Parents evening")
        self.assertTrue(results[0]["unread"])

    def test_read_parses_the_body(self):
        service, messages = self._service()
        raw = (
            "From: School <office@school.example>\r\nTo: ike@example.com\r\n"
            "Subject: Parents evening\r\nDate: Wed, 14 Oct 2026 09:00:00 +0100\r\n\r\n"
            "Parents evening is on Thursday 22 October at 18:00.\r\n"
        ).encode()
        messages.get.return_value.execute.return_value = {"raw": base64.urlsafe_b64encode(raw).decode()}
        with patch.object(personal_inbox, "_service", return_value=service):
            message = personal_inbox.read_message("ike@example.com", "m1")

        self.assertEqual(message["subject"], "Parents evening")
        self.assertIn("Thursday 22 October", message["body"])
        self.assertFalse(message["truncated"])


@override_settings(GOOGLE_CLIENT_ID="client", GOOGLE_CLIENT_SECRET="secret", GOOGLE_CLIENT_SECRETS_FILE="",
                   APP_ENCRYPTION_KEY="kSmS4bmrFtLnz0zXy4Z6mPdkR5tWv8Ow9q3BvO5yFkQ=")
class GoogleAuthReadonlyTests(TestCase):
    def _run(self, signed_in_as, *args):
        credentials = SimpleNamespace(
            token="t", refresh_token="r", token_uri="u", client_id="client",
            client_secret="secret", scopes=[personal_inbox.READONLY_SCOPE],
        )
        flow = MagicMock()
        flow.run_local_server.return_value = credentials
        profile = MagicMock()
        profile.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": signed_in_as}
        with patch("assistant.management.commands.google_auth.InstalledAppFlow.from_client_config", return_value=flow) as make, \
             patch("assistant.management.commands.google_auth.build", return_value=profile):
            call_command("google_auth", *args, stdout=MagicMock())
        return make

    def test_readonly_account_is_stored_with_readonly_scope(self):
        make = self._run("ike.personal@example.com", "--account", "Ike.Personal@example.com", "--readonly")
        self.assertEqual(make.call_args.kwargs["scopes"], [personal_inbox.READONLY_SCOPE])
        record = GoogleCredential.objects.get(account_email="ike.personal@example.com")
        self.assertEqual(record.get_credentials()["scopes"], [personal_inbox.READONLY_SCOPE])

    def test_signing_in_as_someone_else_stores_nothing(self):
        with self.assertRaisesMessage(CommandError, "not ike.personal@example.com"):
            self._run("someone.else@example.com", "--account", "ike.personal@example.com", "--readonly")
        self.assertFalse(GoogleCredential.objects.exists())

    def test_account_requires_readonly(self):
        with self.assertRaises(CommandError):
            call_command("google_auth", "--account", "ike.personal@example.com", stdout=MagicMock())
