import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from assistant.services.extractor import AssistantAction
from assistant.services.telegram_handlers import process_telegram_update
from assistant.services.telegram_transcription import TelegramTranscriptionError
from core.models import (
    IncomingEmail,
    ParsedAction,
    Task,
    TelegramChat,
    TelegramConversation,
    TelegramUpdate,
)


class FakeTelegramBot:
    def __init__(self):
        self.messages = []
        self.callbacks = []
        self.file_requests = []
        self.downloads = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return {"message_id": len(self.messages) + 100}

    def answer_callback(self, callback_query_id, text=""):
        self.callbacks.append((callback_query_id, text))
        return True

    def get_file(self, file_id):
        self.file_requests.append(file_id)
        return {"file_path": "voice/file_1.oga"}

    def download_file(self, file_path, *, max_bytes):
        self.downloads.append((file_path, max_bytes))
        return b"SECRET RAW AUDIO"

    @staticmethod
    def safe_audio_filename(file_path):
        return "telegram-voice.oga"


SETTINGS = {
    "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
    "TELEGRAM_BOT_TOKEN": "test-token",
    "TELEGRAM_IKE_USER_ID": 446464092,
    "TELEGRAM_WIFE_USER_ID": 1115153420,
    "TELEGRAM_ALLOWED_GROUP_CHAT_IDS": [],
    "AUTHORISED_EMAIL_IKE": "ike@example.com",
    "AUTHORISED_EMAIL_WIFE": "wife@example.com",
    "OPENAI_API_KEY": "test-openai-key",
    "OPENAI_MODEL": "test-model",
}


def message_update(update_id, *, user_id=446464092, chat_id=None, chat_type="private", text="/start"):
    chat_id = user_id if chat_id is None else chat_id
    chat = {"id": chat_id, "type": chat_type}
    if chat_type != "private":
        chat["title"] = "Family"
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 10,
            "from": {"id": user_id, "is_bot": False, "first_name": "Ike"},
            "chat": chat,
            "text": text,
        },
    }


def voice_update(
    update_id,
    *,
    user_id=446464092,
    chat_id=None,
    chat_type="private",
    duration=8,
    file_size=2048,
    file_id="voice-file-id",
):
    payload = message_update(
        update_id, user_id=user_id, chat_id=chat_id, chat_type=chat_type, text=""
    )
    payload["message"].pop("text")
    payload["message"]["voice"] = {
        "duration": duration,
        "file_size": file_size,
        "file_id": file_id,
        "mime_type": "audio/ogg",
    }
    return payload


@override_settings(**SETTINGS)
class TelegramWebhookSecurityTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_rejects_missing_or_wrong_secret(self):
        payload = json.dumps(message_update(1))
        self.assertEqual(
            self.client.post("/telegram/webhook/", payload, content_type="application/json").status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/telegram/webhook/",
                payload,
                content_type="application/json",
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="wrong",
            ).status_code,
            403,
        )

    @patch("assistant.views.process_telegram_update")
    def test_accepts_valid_secret(self, process):
        response = self.client.post(
            "/telegram/webhook/",
            json.dumps(message_update(2)),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-webhook-secret",
        )
        self.assertEqual(response.status_code, 200)
        process.assert_called_once()


@override_settings(**SETTINGS)
class TelegramConversationTests(TestCase):
    def setUp(self):
        self.bot = FakeTelegramBot()

    def test_private_start_is_authorised_and_idempotent(self):
        payload = message_update(10)
        process_telegram_update(payload, bot=self.bot)
        process_telegram_update(payload, bot=self.bot)

        chat = TelegramChat.objects.get(chat_id=446464092)
        self.assertTrue(chat.authorised)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(TelegramUpdate.objects.filter(update_id=10, processed=True).count(), 1)
        markup = self.bot.messages[0][2]["reply_markup"]
        self.assertTrue(markup["inline_keyboard"])

    def test_unknown_user_is_ignored_without_calling_bot(self):
        process_telegram_update(message_update(11, user_id=999, chat_id=999), bot=self.bot)
        self.assertEqual(self.bot.messages, [])
        self.assertEqual(TelegramUpdate.objects.get(update_id=11).update_type, "unauthorised")

    def test_owner_can_link_shared_group(self):
        payload = message_update(
            12, chat_id=-1001234567890, chat_type="supergroup", text="/linkgroup"
        )
        process_telegram_update(payload, bot=self.bot)
        self.assertTrue(TelegramChat.objects.get(chat_id=-1001234567890).authorised)
        self.assertIn("linked", self.bot.messages[-1][1])

    def test_natural_language_task_query_uses_read_only_list(self):
        process_telegram_update(message_update(13, text="What tasks do we have?"), bot=self.bot)
        self.assertIn("No open tasks", self.bot.messages[-1][1])
        self.assertEqual(TelegramConversation.objects.count(), 0)

    @patch("assistant.services.telegram_handlers.extract_telegram_action")
    def test_missing_time_prompts_then_builds_confirmation(self, extract):
        extract.side_effect = [
            AssistantAction(
                action_type="create_calendar_event",
                title="Dentist",
                appointment_date="2026-08-12",
                confidence=0.95,
                missing_fields=["start_time"],
            ),
            AssistantAction(
                action_type="create_calendar_event",
                title="Dentist",
                appointment_date="2026-08-12",
                start_time="09:00",
                confidence=0.97,
            ),
        ]
        process_telegram_update(message_update(20, text="Dentist next Wednesday"), bot=self.bot)
        conversation = TelegramConversation.objects.get()
        self.assertEqual(conversation.status, TelegramConversation.Status.AWAITING_CLARIFICATION)
        self.assertIn("What time", self.bot.messages[-1][1])

        process_telegram_update(message_update(21, text="9am"), bot=self.bot)
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, TelegramConversation.Status.AWAITING_CONFIRMATION)
        self.assertIsNotNone(conversation.parsed_action)
        self.assertEqual(conversation.parsed_action.incoming_email.source, IncomingEmail.Source.TELEGRAM)
        self.assertIn("Is this right?", self.bot.messages[-1][1])

    @patch("assistant.services.telegram_handlers.admin_approve_and_execute")
    @patch("assistant.services.telegram_handlers.extract_telegram_action")
    def test_confirmation_executes_once(self, extract, execute):
        extract.return_value = AssistantAction(
            action_type="create_task",
            title="Buy milk",
            assigned_to="ike",
            confidence=0.99,
        )
        process_telegram_update(message_update(30, text="Add buy milk to my tasks"), bot=self.bot)
        conversation = TelegramConversation.objects.get()
        execute.return_value = ("task_created", {"task": type("TaskResult", (), {"title": "Buy milk"})()})
        callback = {
            "update_id": 31,
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 446464092, "is_bot": False, "first_name": "Ike"},
                "data": f"tg:confirm:{conversation.id}",
                "message": {
                    "message_id": 101,
                    "chat": {"id": 446464092, "type": "private"},
                },
            },
        }
        process_telegram_update(callback, bot=self.bot)
        process_telegram_update(callback, bot=self.bot)
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, TelegramConversation.Status.COMPLETED)
        self.assertEqual(conversation.transcript, [])
        execute.assert_called_once()

    @patch("assistant.services.telegram_handlers.extract_telegram_action")
    def test_confirmed_task_uses_existing_task_service(self, extract):
        extract.return_value = AssistantAction(
            action_type="create_task",
            title="Book boiler service",
            assigned_to="both",
            confidence=0.99,
        )
        process_telegram_update(message_update(40, text="Book the boiler service"), bot=self.bot)
        conversation = TelegramConversation.objects.get()
        process_telegram_update(
            {
                "update_id": 41,
                "callback_query": {
                    "id": "callback-real-task",
                    "from": {"id": 446464092, "is_bot": False, "first_name": "Ike"},
                    "data": f"tg:confirm:{conversation.id}",
                    "message": {
                        "message_id": 102,
                        "chat": {"id": 446464092, "type": "private"},
                    },
                },
            },
            bot=self.bot,
        )
        self.assertEqual(Task.objects.filter(title="Book boiler service", assigned_to="both").count(), 1)
        conversation.refresh_from_db()
        self.assertEqual(conversation.parsed_action.status, ParsedAction.Status.EXECUTED)

    def test_email_source_default_is_unchanged(self):
        incoming = IncomingEmail.objects.create(
            gmail_message_id="normal-gmail-message",
            outer_sender="ike@example.com",
        )
        self.assertEqual(incoming.source, IncomingEmail.Source.EMAIL)

    @patch("assistant.services.telegram_handlers.extract_telegram_action")
    @patch("assistant.services.telegram_handlers.transcribe_telegram_voice")
    def test_authorised_voice_is_transcribed_and_routed_to_text_flow(self, transcribe, extract):
        transcribe.return_value = "Remind both of us on Friday at 6pm to renew <insurance>"
        extract.return_value = AssistantAction(
            action_type="create_email_reminder",
            title="Renew insurance",
            reminder_date="2026-09-18",
            reminder_time="18:00",
            reminder_recipient="both",
            confidence=0.98,
        )

        process_telegram_update(voice_update(50), bot=self.bot)

        conversation = TelegramConversation.objects.get()
        self.assertEqual(conversation.status, TelegramConversation.Status.AWAITING_CONFIRMATION)
        self.assertEqual(
            conversation.transcript[0]["text"],
            "Remind both of us on Friday at 6pm to renew <insurance>",
        )
        self.assertIn("&lt;insurance&gt;", self.bot.messages[0][1])
        self.assertIn("I heard", self.bot.messages[0][1])
        self.assertIn("Is this right?", self.bot.messages[-1][1])
        self.assertEqual(self.bot.file_requests, ["voice-file-id"])
        transcribe.assert_called_once()

    @patch("assistant.services.telegram_handlers.transcribe_telegram_voice")
    def test_unauthorised_voice_never_downloads_or_transcribes(self, transcribe):
        process_telegram_update(voice_update(51, user_id=999, chat_id=999), bot=self.bot)

        self.assertEqual(self.bot.file_requests, [])
        self.assertEqual(self.bot.downloads, [])
        transcribe.assert_not_called()
        self.assertEqual(TelegramUpdate.objects.get(update_id=51).update_type, "unauthorised")

    @patch("assistant.services.telegram_handlers.extract_telegram_action")
    @patch("assistant.services.telegram_handlers.transcribe_telegram_voice", return_value="9am")
    def test_voice_can_answer_an_active_clarification(self, transcribe, extract):
        extract.side_effect = [
            AssistantAction(
                action_type="create_calendar_event",
                title="Dentist",
                appointment_date="2026-09-18",
                confidence=0.9,
                missing_fields=["start_time"],
            ),
            AssistantAction(
                action_type="create_calendar_event",
                title="Dentist",
                appointment_date="2026-09-18",
                start_time="09:00",
                confidence=0.98,
            ),
        ]
        process_telegram_update(message_update(52, text="Dentist on Friday"), bot=self.bot)
        process_telegram_update(voice_update(53), bot=self.bot)

        conversation = TelegramConversation.objects.get()
        self.assertEqual(conversation.status, TelegramConversation.Status.AWAITING_CONFIRMATION)
        self.assertEqual(conversation.transcript[-1], {"role": "user", "text": "9am"})
        transcribe.assert_called_once()

    @patch("assistant.services.telegram_handlers.transcribe_telegram_voice")
    def test_oversized_voice_notes_are_rejected_before_download(self, transcribe):
        process_telegram_update(voice_update(54, duration=121), bot=self.bot)
        process_telegram_update(voice_update(55, file_size=5 * 1024 * 1024 + 1), bot=self.bot)

        self.assertEqual(self.bot.file_requests, [])
        self.assertEqual(self.bot.downloads, [])
        transcribe.assert_not_called()
        self.assertIn("over 2 minutes", self.bot.messages[0][1])
        self.assertIn("over 5 MB", self.bot.messages[1][1])

    @patch("assistant.services.telegram_handlers.transcribe_telegram_voice")
    def test_failed_or_empty_transcription_is_friendly_and_creates_no_action(self, transcribe):
        transcribe.side_effect = TelegramTranscriptionError("must not be shown")
        process_telegram_update(voice_update(56), bot=self.bot)

        self.assertIn("couldn’t transcribe", self.bot.messages[-1][1])
        self.assertNotIn("must not be shown", self.bot.messages[-1][1])
        self.assertEqual(TelegramConversation.objects.count(), 0)
        self.assertEqual(ParsedAction.objects.count(), 0)
        self.assertTrue(TelegramUpdate.objects.get(update_id=56).processed)

    @patch("assistant.services.telegram_handlers.extract_telegram_action")
    @patch("assistant.services.telegram_handlers.transcribe_telegram_voice", return_value="Buy milk")
    def test_voice_audio_is_not_persisted_and_duplicate_processes_once(self, transcribe, extract):
        extract.return_value = AssistantAction(
            action_type="create_task",
            title="Buy milk",
            assigned_to="ike",
            confidence=0.99,
        )
        payload = voice_update(57)
        process_telegram_update(payload, bot=self.bot)
        process_telegram_update(payload, bot=self.bot)

        self.assertEqual(self.bot.file_requests, ["voice-file-id"])
        self.assertEqual(len(self.bot.downloads), 1)
        transcribe.assert_called_once()
        extract.assert_called_once()
        self.assertNotIn("SECRET RAW AUDIO", IncomingEmail.objects.get().body_text)
        self.assertNotIn("SECRET RAW AUDIO", str(TelegramConversation.objects.get().transcript))
        update = TelegramUpdate.objects.get(update_id=57)
        self.assertNotIn("SECRET RAW AUDIO", update.error)
        self.assertTrue(update.processed)

    @patch("assistant.services.telegram_handlers.transcribe_telegram_voice")
    def test_in_progress_duplicate_does_not_download_or_transcribe(self, transcribe):
        TelegramUpdate.objects.create(update_id=58, processing_started_at=timezone.now())

        process_telegram_update(voice_update(58), bot=self.bot)

        self.assertEqual(self.bot.file_requests, [])
        self.assertEqual(self.bot.downloads, [])
        transcribe.assert_not_called()
        self.assertFalse(TelegramUpdate.objects.get(update_id=58).processed)
