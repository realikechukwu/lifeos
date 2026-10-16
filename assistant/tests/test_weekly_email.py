from datetime import date, datetime, time
from unittest.mock import patch

import anthropic
import httpx2
from django.test import TestCase, override_settings
from django.utils import timezone

from assistant.agent.weekly_email import INBOX_UNAVAILABLE, send_due_weekly_emails
from assistant.agent.worker import claim_next_jobs, run_jobs
from assistant.management.commands.send_telegram_briefings import send_due_briefings
from assistant.tests.test_agent import AGENT_SETTINGS, AgentFakeBot, FakeClient, reply, text, tool_use
from assistant.tests.test_agent_email import FakeGmail as EmailFakeGmail, raw_email
from core.models import AgentJob, AgentThread, Task, TelegramChat, TelegramPreference, TelegramUser, WeeklyEmailBriefing

WEEKLY_SETTINGS = {
    **AGENT_SETTINGS,
    "WEEKLY_EMAIL_ENABLED": True,
    "AGENT_PERSONAL_INBOX_ENABLED": True,
    "PERSONAL_GMAIL_IKE": "ike.personal@example.com",
    "PERSONAL_GMAIL_WIFE": "",
    "AGENT_EMAIL_ENABLED": True,
    "AGENT_PROPOSALS_ENABLED": True,
    "GOOGLE_ASSISTANT_EMAIL": "assistant@example.com",
}
LONDON = timezone.get_default_timezone()
SUNDAY_4PM = datetime(2026, 10, 18, 16, 0, tzinfo=LONDON)
SHIFT = {"title": "Ike — Late shift", "date": "2026-10-19", "start_time": "15:00", "end_time": "23:00"}
INBOX = [{"message_id": "m1", "from": "school@example.com", "subject": "Trip form due Wednesday", "snippet": "Please return"}]
SHARED = "Day by day:\n- Mon 19: Ike late 15:00–23:00"


class FakeGmail(EmailFakeGmail):
    def send_new_thread(self, **kwargs):
        self.sent.append(kwargs)
        return f"new-{len(self.sent)}", f"gmail-thread-{len(self.sent)}"


def bad_request():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError("bad", response=httpx2.Response(400, request=request), body=None)


@override_settings(**WEEKLY_SETTINGS)
@patch("assistant.agent.tools.personal_inbox.search_messages", return_value=INBOX)
@patch("assistant.agent.tools.list_calendar_events", return_value=[SHIFT])
class WeeklyEmailTests(TestCase):
    def setUp(self):
        self.gmail = FakeGmail()

    def run_worker(self, client):
        while jobs := claim_next_jobs():
            run_jobs(jobs, client=client, gmail_service=self.gmail)

    def sent_to(self, address):
        return next(m for m in self.gmail.sent if m["to_addr"] == address)

    def test_sunday_four_pm_queues_one_email_per_adult_once(self, *_):
        self.assertEqual(send_due_weekly_emails(now=SUNDAY_4PM), (2, 0))
        self.assertEqual(send_due_weekly_emails(now=SUNDAY_4PM), (0, 0))

        jobs = AgentJob.objects.filter(channel="email", briefing_type="weekly")
        self.assertEqual(
            [j.reply_metadata for j in jobs],
            [{"role": "ike", "week_start": "2026-10-19"}, {"role": "wife", "week_start": "2026-10-19"}],
        )

    def test_nothing_is_due_at_other_times(self, *_):
        self.assertEqual(send_due_weekly_emails(now=SUNDAY_4PM.replace(hour=18)), (0, 0))
        self.assertEqual(send_due_weekly_emails(now=SUNDAY_4PM.replace(day=17)), (0, 0))  # Saturday
        with self.settings(WEEKLY_EMAIL_ENABLED=False):
            self.assertEqual(send_due_weekly_emails(now=SUNDAY_4PM), (0, 0))
        self.assertFalse(WeeklyEmailBriefing.objects.exists())

    def test_the_sunday_telegram_plan_is_replaced(self, *_):
        user = TelegramUser.objects.create(user_id=446464092, role="ike", display_name="Ike")
        TelegramChat.objects.create(chat_id=446464092, chat_type="private", authorised=True)
        TelegramPreference.objects.create(user=user, briefing_time=time(7, 30))
        bot = AgentFakeBot()

        self.assertEqual(send_due_briefings(bot=bot, now=SUNDAY_4PM.replace(hour=18)), (0, 0))
        with self.settings(WEEKLY_EMAIL_ENABLED=False):
            self.assertEqual(send_due_briefings(bot=bot, now=SUNDAY_4PM.replace(hour=18)), (1, 0))

    def test_shared_section_is_written_once_and_inbox_stays_private(self, _calendar, search):
        send_due_weekly_emails(now=SUNDAY_4PM)
        client = FakeClient(
            reply(text(SHARED)),
            reply(tool_use("search_my_inbox", {"query": "newer_than:7d"}), stop_reason="tool_use"),
            reply(text("- School trip form: return by Wed 21")),
        )
        self.run_worker(client)

        self.assertEqual(len(client.calls), 3)  # one shared call, one inbox turn for Ike only
        shared_call = client.calls[0]
        self.assertNotIn("tools", shared_call)
        shared_prompt = shared_call["messages"][0]["content"][0]["text"]
        self.assertIn("Ike — Late shift", shared_prompt)
        self.assertNotIn("Trip form", shared_prompt)
        search.assert_called_once_with("ike.personal@example.com", "newer_than:7d", max_results=10)

        ike, wife = self.sent_to("ike@example.com"), self.sent_to("wife@example.com")
        self.assertEqual(ike["subject"], "Your week ahead: Mon 19 Oct to Sun 25 Oct")
        for email in (ike, wife):
            self.assertIn("FOR BOTH OF YOU\n\n" + SHARED, email["body_text"])
        self.assertIn("FROM YOUR INBOX\n\n- School trip form", ike["body_text"])
        self.assertNotIn("FROM YOUR INBOX", wife["body_text"])
        self.assertNotIn("School trip", wife["body_text"])
        self.assertEqual(AgentJob.objects.filter(status=AgentJob.Status.DONE).count(), 2)

        thread = AgentThread.objects.get()
        self.assertEqual((thread.role, thread.email_thread_id), ("ike", "gmail-thread-1"))

    def test_a_reply_continues_the_weekly_conversation(self, *_):
        send_due_weekly_emails(now=SUNDAY_4PM)
        self.run_worker(FakeClient(reply(text(SHARED)), reply(text("- School trip form: return by Wed 21"))))
        weekly_thread = AgentThread.objects.get()

        self.gmail.messages = {"reply-1": (raw_email("Add the trip form as a task").as_bytes(), "gmail-thread-1")}
        with patch("assistant.management.commands.poll_gmail.GmailService", return_value=self.gmail), \
             patch("assistant.management.commands.poll_gmail.extract_actions"):
            from django.core.management import call_command
            from io import StringIO
            call_command("poll_gmail", stdout=StringIO())
        client = FakeClient(reply(text("Which day should it be due?")))
        self.run_worker(client)

        self.assertEqual(AgentJob.objects.get(incoming_email__gmail_message_id="reply-1").thread, weekly_thread)
        history = client.calls[0]["messages"]
        self.assertIn("Scheduled week-ahead email", history[0]["content"][0]["text"])

    def test_the_scheduled_turn_cannot_create_anything(self, *_):
        send_due_weekly_emails(now=SUNDAY_4PM)
        client = FakeClient(
            reply(text(SHARED)),
            reply(tool_use("propose_task", {"title": "Return trip form"}), stop_reason="tool_use"),
            reply(text("- School trip form: return by Wed 21")),
        )
        self.run_worker(client)

        tool_result = client.calls[2]["messages"][-1]["content"][0]
        self.assertTrue(tool_result["is_error"])
        self.assertFalse(Task.objects.exists())

    def test_agent_failure_still_sends_the_template_week(self, *_):
        Task.objects.create(title="Pay council tax", due_date=date(2026, 10, 20))
        send_due_weekly_emails(now=SUNDAY_4PM)
        client = FakeClient(bad_request())
        self.run_worker(client)

        # Ike's job fails and stores the template; the wife's email reuses it without a model call.
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            list(AgentJob.objects.values_list("status", flat=True)), [AgentJob.Status.FAILED, AgentJob.Status.DONE]
        )
        ike, wife = self.sent_to("ike@example.com"), self.sent_to("wife@example.com")
        self.assertIn("Tue 20 Oct 2026 — Pay council tax", ike["body_text"])
        self.assertIn(INBOX_UNAVAILABLE, ike["body_text"])
        self.assertNotIn(INBOX_UNAVAILABLE, wife["body_text"])  # no inbox connected
        shared = WeeklyEmailBriefing.objects.get().shared_text
        self.assertIn(shared, wife["body_text"])
        self.assertNotIn("<b>", shared)

    @override_settings(AGENT_ENABLED=False)
    def test_without_the_agent_template_emails_go_straight_out(self, *_):
        self.assertEqual(send_due_weekly_emails(now=SUNDAY_4PM, gmail_service=self.gmail), (2, 0))

        self.assertFalse(AgentJob.objects.exists())
        self.assertEqual([m["to_addr"] for m in self.gmail.sent], ["ike@example.com", "wife@example.com"])
        self.assertNotIn("Reply to this email", self.gmail.sent[0]["body_text"])
