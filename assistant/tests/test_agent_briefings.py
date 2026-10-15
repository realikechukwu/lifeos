from datetime import datetime, time

import anthropic
import httpx2
from django.test import TestCase, override_settings
from django.utils import timezone

from assistant.agent.briefings import SCHEDULED_NOTE
from assistant.agent.worker import claim_next_jobs, run_jobs
from assistant.management.commands.send_telegram_briefings import send_due_briefings
from assistant.tests.test_agent import AGENT_SETTINGS, AgentFakeBot, FakeClient, reply, text
from core.models import AgentJob, TelegramBriefingDelivery, TelegramChat, TelegramPreference, TelegramUser

BRIEFING_SETTINGS = {**AGENT_SETTINGS, "AGENT_BRIEFINGS_ENABLED": True}
LONDON = timezone.get_default_timezone()


def _at(*args):
    return datetime(*args, tzinfo=LONDON)


@override_settings(**BRIEFING_SETTINGS)
class AgentBriefingTests(TestCase):
    def setUp(self):
        self.user = TelegramUser.objects.create(user_id=446464092, role="ike", display_name="Ike")
        self.chat = TelegramChat.objects.create(chat_id=446464092, chat_type="private", authorised=True)
        TelegramPreference.objects.create(user=self.user, briefing_time=time(7, 30))
        self.bot = AgentFakeBot()

    def test_daily_brief_is_queued_for_the_agent_once(self):
        now = _at(2026, 10, 16, 7, 30)  # Friday
        self.assertEqual(send_due_briefings(bot=self.bot, now=now), (1, 0))
        self.assertEqual(send_due_briefings(bot=self.bot, now=now), (0, 0))

        job = AgentJob.objects.get()
        self.assertEqual(job.briefing_type, "daily")
        self.assertIn("Friday 16 October 2026", job.text)
        self.assertIn("search_my_inbox", job.text)
        self.assertEqual(self.bot.messages, [])  # the template is not sent
        self.assertEqual(TelegramBriefingDelivery.objects.count(), 1)

    def test_sunday_evening_queues_the_week_ahead_plan(self):
        send_due_briefings(bot=self.bot, now=_at(2026, 10, 18, 18, 0))  # Sunday
        job = AgentJob.objects.get(briefing_type="weekly")
        self.assertIn("Monday 19 October to Sunday 25 October 2026", job.text)

    def test_monthly_briefing_stays_a_template(self):
        TelegramPreference.objects.update(briefing_time=time(6, 0))
        sent, _ = send_due_briefings(bot=self.bot, now=_at(2026, 11, 1, 9, 30))  # first Sunday
        self.assertEqual(sent, 1)
        self.assertFalse(AgentJob.objects.exists())
        self.assertEqual(len(self.bot.messages), 1)

    @override_settings(AGENT_BRIEFINGS_ENABLED=False)
    def test_switched_off_sends_the_template(self):
        send_due_briefings(bot=self.bot, now=_at(2026, 10, 16, 7, 30))
        self.assertFalse(AgentJob.objects.exists())
        self.assertEqual(len(self.bot.messages), 1)

    def test_worker_writes_the_brief_in_the_private_chat(self):
        send_due_briefings(bot=self.bot, now=_at(2026, 10, 16, 7, 30))
        # A message typed at the same moment is answered separately.
        AgentJob.objects.create(
            channel="telegram", telegram_chat=self.chat, telegram_user=self.user,
            telegram_update_id=77, text="Thanks!",
        )
        client = FakeClient(reply(text("**Today**: Late shift 15:00–23:00.")))
        jobs = claim_next_jobs()
        self.assertEqual([j.briefing_type for j in jobs], ["daily"])
        run_jobs(jobs, bot=self.bot, client=client)

        messages = client.calls[0]["messages"]
        self.assertIn("Scheduled morning brief", messages[0]["content"][0]["text"])
        self.assertIn(SCHEDULED_NOTE, messages[1]["content"])
        self.assertIn("<b>Today</b>", self.bot.messages[-1][1])
        self.assertEqual(AgentJob.objects.get(briefing_type="daily").status, AgentJob.Status.DONE)

    def test_agent_failure_falls_back_to_the_template_brief(self):
        send_due_briefings(bot=self.bot, now=_at(2026, 10, 16, 7, 30))
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        bad = anthropic.BadRequestError("bad", response=httpx2.Response(400, request=request), body=None)
        run_jobs(claim_next_jobs(), bot=self.bot, client=FakeClient(bad))

        self.assertEqual(AgentJob.objects.get().status, AgentJob.Status.FAILED)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertNotIn("something went wrong", self.bot.messages[0][1])
