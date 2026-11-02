import base64
from unittest.mock import Mock

from django.test import TestCase, override_settings

from assistant.agent.photos import MAX_PHOTO_BYTES
from assistant.agent.worker import claim_next_jobs, run_jobs
from assistant.services.telegram_handlers import process_telegram_update
from assistant.tests.test_agent import AGENT_SETTINGS, AgentFakeBot, FakeClient, reply, text
from assistant.tests.test_telegram import message_update
from core.models import AgentJob, AuditLog

JPEG = b"\xff\xd8\xff\xe0 fake jpeg"


def photo_update(update_id, *, caption=None, sizes=None, **kwargs):
    payload = message_update(update_id, text="", **kwargs)
    payload["message"].pop("text")
    payload["message"]["photo"] = sizes or [
        {"file_id": "small", "width": 90, "height": 67, "file_size": 1200},
        {"file_id": "large", "width": 1280, "height": 960, "file_size": 180000},
    ]
    if caption is not None:
        payload["message"]["caption"] = caption
    return payload


@override_settings(**AGENT_SETTINGS, AGENT_PHOTOS_ENABLED=True)
class PhotoTests(TestCase):
    def setUp(self):
        self.bot = AgentFakeBot()
        self.bot.get_file = Mock(return_value={"file_path": "photos/file_1.jpg"})
        self.bot.download_file = Mock(return_value=JPEG)

    def test_photo_queues_largest_size_as_image_block_with_caption(self):
        process_telegram_update(photo_update(1, caption="Add this to the calendar"), bot=self.bot)
        job = AgentJob.objects.get()
        self.bot.get_file.assert_called_once_with("large")
        self.bot.download_file.assert_called_once_with("photos/file_1.jpg", max_bytes=MAX_PHOTO_BYTES)
        self.assertEqual(job.text, "Add this to the calendar")
        image = job.input_blocks[-1]
        self.assertEqual((image["type"], image["source"]["media_type"]), ("image", "image/jpeg"))
        self.assertEqual(base64.b64decode(image["source"]["data"]), JPEG)
        self.assertTrue(AuditLog.objects.filter(action="agent_photo_received").exists())

    def test_photo_without_caption_gets_a_default_prompt(self):
        process_telegram_update(photo_update(2), bot=self.bot)
        self.assertEqual(AgentJob.objects.get().input_blocks[0]["text"], "I've sent a photo.")

    def test_image_sent_as_file_keeps_its_media_type(self):
        payload = message_update(3, text="")
        payload["message"]["document"] = {"file_id": "doc", "file_name": "letter.png", "mime_type": "image/png", "file_size": 5000}
        process_telegram_update(payload, bot=self.bot)
        self.bot.get_file.assert_called_once_with("doc")
        self.assertEqual(AgentJob.objects.get().input_blocks[-1]["source"]["media_type"], "image/png")

    def test_oversized_image_rejected_before_download(self):
        payload = message_update(4, text="")
        payload["message"]["document"] = {"file_id": "doc", "mime_type": "image/jpeg", "file_size": MAX_PHOTO_BYTES + 1}
        process_telegram_update(payload, bot=self.bot)
        self.bot.get_file.assert_not_called()
        self.assertIn("5 MB", self.bot.messages[-1][1])
        self.assertFalse(AgentJob.objects.exists())

    def test_redelivered_update_is_queued_once(self):
        process_telegram_update(photo_update(5), bot=self.bot)
        process_telegram_update(photo_update(5), bot=self.bot)
        self.assertEqual(AgentJob.objects.count(), 1)

    def test_group_photo_needs_a_mention(self):
        process_telegram_update(message_update(6, chat_id=-500, chat_type="group", text="/linkgroup"), bot=self.bot)
        process_telegram_update(photo_update(7, chat_id=-500, chat_type="group", caption="Holiday"), bot=self.bot)
        self.assertFalse(AgentJob.objects.exists())
        process_telegram_update(photo_update(8, chat_id=-500, chat_type="group", caption="@LifeOSBot add this"), bot=self.bot)
        self.assertEqual(AgentJob.objects.get().text, "add this")

    @override_settings(AGENT_PHOTOS_ENABLED=False)
    def test_flag_off_ignores_photos(self):
        process_telegram_update(photo_update(9, caption="Add this"), bot=self.bot)
        self.bot.get_file.assert_not_called()
        self.assertFalse(AgentJob.objects.exists())

    def test_worker_sends_the_image_to_claude(self):
        process_telegram_update(photo_update(10, caption="What is this?"), bot=self.bot)
        client = FakeClient(reply(text("A dentist appointment card for Fri 14 Nov.")))
        run_jobs(claim_next_jobs(), bot=self.bot, client=client)
        content = client.calls[0]["messages"][0]["content"]
        self.assertEqual(content[-1]["type"], "image")
        self.assertEqual(self.bot.messages[-1][1], "A dentist appointment card for Fri 14 Nov.")
