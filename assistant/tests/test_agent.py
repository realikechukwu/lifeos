import copy
import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx2
from anthropic.types.beta import BetaMessage
from django.test import TestCase, override_settings
from django.utils import timezone

from assistant.agent.formatting import split_message, to_telegram_html
from assistant.agent.loop import INTERRUPTED_RESULT, REFUSAL_REPLY
from assistant.agent.worker import claim_next_jobs, requeue_orphaned_jobs, run_jobs
from assistant.services.telegram_handlers import process_telegram_update
from assistant.tests.test_telegram import SETTINGS, FakeTelegramBot, message_update, voice_update
from core.models import (
    AgentJob,
    AgentMemory,
    AgentMessage,
    AgentThread,
    IncomingEmail,
    Task,
    TelegramChat,
    TelegramConversation,
    TelegramUser,
)

BOT_ID = 999000
AGENT_SETTINGS = {
    **SETTINGS,
    "AGENT_ENABLED": True,
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "AGENT_MODEL": "claude-sonnet-5-5",
    "AGENT_EFFORT": "medium",
    "AGENT_MAX_TOOL_ROUNDS": 12,
    "AGENT_THREAD_IDLE_HOURS": 8,
}


class AgentFakeBot(FakeTelegramBot):
    def __init__(self):
        super().__init__()
        self.actions = []

    def get_me(self):
        return {"id": BOT_ID, "username": "LifeOSBot"}

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))
        return True


def reply(*blocks, stop_reason="end_turn"):
    return BetaMessage.model_validate({
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": list(blocks),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 12,
            "output_tokens": 7,
            "cache_read_input_tokens": 3000,
            "cache_creation_input_tokens": 40,
        },
    })


def text(value):
    return {"type": "text", "text": value}


def tool_use(name, tool_input, tool_id="toolu_1"):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, *responses):
        self.beta = SimpleNamespace(messages=FakeMessages(responses))

    @property
    def calls(self):
        return self.beta.messages.calls


def server_error():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.InternalServerError("overloaded", response=httpx2.Response(500, request=request), body=None)


@override_settings(**AGENT_SETTINGS)
class AgentRoutingTests(TestCase):
    def setUp(self):
        self.bot = AgentFakeBot()

    def test_private_text_is_queued_without_calling_any_model(self):
        with patch("assistant.services.telegram_handlers.extract_telegram_action") as extract:
            process_telegram_update(message_update(1, text="Am I free on Friday?"), bot=self.bot)

        extract.assert_not_called()
        job = AgentJob.objects.get()
        self.assertEqual(job.status, AgentJob.Status.QUEUED)
        self.assertEqual(job.text, "Am I free on Friday?")
        self.assertEqual(job.telegram_update_id, 1)
        self.assertEqual(self.bot.messages, [])

    def test_redelivered_update_is_queued_once(self):
        process_telegram_update(message_update(2, text="hello"), bot=self.bot)
        AgentJob.objects.update(telegram_update_id=2)
        from assistant.agent.telegram import enqueue_telegram_message

        enqueue_telegram_message(
            chat=TelegramChat.objects.get(), user=TelegramUser.objects.get(),
            text="hello", update_id=2, message_id=12,
        )
        self.assertEqual(AgentJob.objects.count(), 1)

    def _link_group(self):
        process_telegram_update(
            message_update(10, chat_id=-500, chat_type="group", text="/linkgroup"), bot=self.bot
        )

    def test_group_message_not_addressed_to_bot_is_ignored(self):
        self._link_group()
        process_telegram_update(
            message_update(11, chat_id=-500, chat_type="group", text="Dinner at 7?"), bot=self.bot
        )
        self.assertFalse(AgentJob.objects.exists())

    def test_group_mention_is_queued_without_the_mention(self):
        self._link_group()
        process_telegram_update(
            message_update(12, chat_id=-500, chat_type="group", text="@lifeosbot what's on tomorrow?"),
            bot=self.bot,
        )
        job = AgentJob.objects.get()
        self.assertEqual(job.text, "what's on tomorrow?")
        self.assertEqual(job.telegram_message_id, 22)

    def test_group_reply_to_bot_is_queued(self):
        self._link_group()
        payload = message_update(13, chat_id=-500, chat_type="group", text="and Saturday?")
        payload["message"]["reply_to_message"] = {"message_id": 5, "from": {"id": BOT_ID, "is_bot": True}}
        process_telegram_update(payload, bot=self.bot)
        self.assertEqual(AgentJob.objects.get().text, "and Saturday?")

    def test_unaddressed_group_voice_note_is_not_transcribed(self):
        self._link_group()
        with patch("assistant.services.telegram_handlers.transcribe_telegram_voice") as transcribe:
            process_telegram_update(voice_update(14, chat_id=-500, chat_type="group"), bot=self.bot)
        transcribe.assert_not_called()
        self.assertFalse(AgentJob.objects.exists())

    def test_new_command_closes_the_open_thread(self):
        process_telegram_update(message_update(15, text="/start"), bot=self.bot)
        chat, user = TelegramChat.objects.get(), TelegramUser.objects.get()
        thread = AgentThread.objects.create(
            channel="telegram", role="ike", telegram_chat=chat, telegram_user=user,
            last_activity_at=timezone.now(),
        )
        process_telegram_update(message_update(16, text="/new"), bot=self.bot)
        thread.refresh_from_db()
        self.assertIsNotNone(thread.closed_at)
        self.assertIn("Fresh start", self.bot.messages[-1][1])

    @override_settings(AGENT_ENABLED=False)
    def test_disabled_agent_keeps_the_original_flow(self):
        with patch(
            "assistant.services.telegram_handlers.extract_telegram_action", side_effect=ValueError("stop")
        ) as extract:
            with self.assertRaises(ValueError):
                process_telegram_update(message_update(17, text="Add a dentist trip"), bot=self.bot)
        extract.assert_called_once()
        self.assertFalse(AgentJob.objects.exists())


@override_settings(**AGENT_SETTINGS)
class AgentWorkerTests(TestCase):
    def setUp(self):
        self.bot = AgentFakeBot()

    def _queue(self, update_id, message_text):
        process_telegram_update(message_update(update_id, text=message_text), bot=self.bot)

    def _run(self, client):
        jobs = claim_next_jobs()
        self.assertTrue(jobs)
        run_jobs(jobs, bot=self.bot, client=client)
        return jobs

    @patch("assistant.agent.tools.list_calendar_events")
    def test_calendar_question_runs_tool_and_replies(self, list_events):
        list_events.return_value = [{
            "title": "Ike — Night shift", "all_day": False, "start": "2026-10-16T19:00:00+01:00",
            "end": "2026-10-17T07:00:00+01:00", "location": "", "description": "", "recurring": False,
        }]
        self._queue(20, "Am I free Friday evening?")
        client = FakeClient(
            reply(
                {"type": "thinking", "thinking": "", "signature": "sig-1"},
                tool_use("get_calendar", {"start_date": "2026-10-16", "end_date": "2026-10-16", "search": ""}),
                stop_reason="tool_use",
            ),
            reply(text("No — you're on a **night shift** from 19:00 <until> 07:00.")),
        )

        self._run(client)

        list_events.assert_called_once_with(date(2026, 10, 16), date(2026, 10, 17), query="")
        sent = self.bot.messages[-1][1]
        self.assertIn("<b>night shift</b>", sent)
        self.assertIn("&lt;until&gt;", sent)
        self.assertTrue(self.bot.actions)

        job = AgentJob.objects.get()
        self.assertEqual(job.status, AgentJob.Status.DONE)
        self.assertEqual(job.text, "")

        roles = list(AgentMessage.objects.values_list("role", flat=True))
        self.assertEqual(roles, ["user", "system", "assistant", "user", "assistant"])
        tool_result = AgentMessage.objects.filter(role="user").last().content[0]
        self.assertEqual(tool_result["tool_use_id"], "toolu_1")
        self.assertIn("Night shift", tool_result["content"])
        final = AgentMessage.objects.last()
        self.assertEqual(final.cache_read_tokens, 3000)

        first, second = client.calls
        self.assertEqual(first["model"], "claude-sonnet-5-5")
        self.assertEqual(first["system"][0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertEqual(first["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(first["output_config"], {"effort": "medium"})
        self.assertNotIn("tool_choice", first)
        self.assertEqual([t["name"] for t in first["tools"]], sorted(t["name"] for t in first["tools"]))
        context = first["messages"][1]
        self.assertEqual(context["role"], "system")
        self.assertIn("Speaker: Ike", context["content"])
        self.assertIn("No saved memories", context["content"])
        # The second request replays the first verbatim, thinking block included.
        self.assertEqual(second["messages"][:2], first["messages"])
        self.assertEqual(second["messages"][2]["content"][0]["signature"], "sig-1")
        self.assertEqual(second["system"], first["system"])
        self.assertEqual(second["tools"], first["tools"])

    def test_follow_up_continues_the_same_thread(self):
        self._queue(21, "Hi")
        self._run(FakeClient(reply(text("Hello!"))))
        self._queue(22, "What can you do?")
        client = FakeClient(reply(text("Lots.")))
        self._run(client)

        self.assertEqual(AgentThread.objects.count(), 1)
        messages = client.calls[0]["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "system", "assistant", "user", "system"])
        self.assertNotIn("saved memories", messages[-1]["content"].lower())

    def test_idle_thread_is_replaced(self):
        self._queue(23, "Hi")
        self._run(FakeClient(reply(text("Hello!"))))
        AgentThread.objects.update(last_activity_at=timezone.now() - timedelta(hours=9))
        self._queue(24, "Morning")
        client = FakeClient(reply(text("Morning!")))
        self._run(client)

        self.assertEqual(AgentThread.objects.count(), 2)
        self.assertEqual(AgentThread.objects.filter(closed_at__isnull=True).count(), 1)
        self.assertEqual(len(client.calls[0]["messages"]), 2)

    def test_quick_messages_are_answered_as_one_turn(self):
        self._queue(25, "Dentist next week")
        self._queue(26, "actually make that Tuesday")
        client = FakeClient(reply(text("Got it.")))
        jobs = self._run(client)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            client.calls[0]["messages"][0]["content"][0]["text"],
            "Dentist next week\n\nactually make that Tuesday",
        )
        self.assertEqual(AgentJob.objects.filter(status=AgentJob.Status.DONE).count(), 2)
        self.assertEqual(len(self.bot.messages), 1)

    def test_transient_failure_retries_without_repeating_input(self):
        self._queue(27, "What's on today?")
        self._run(FakeClient(server_error()))

        job = AgentJob.objects.get()
        self.assertEqual(job.status, AgentJob.Status.QUEUED)
        self.assertIsNotNone(job.thread_id)
        self.assertEqual(self.bot.messages, [])

        AgentJob.objects.update(started_at=timezone.now() - timedelta(minutes=1))
        client = FakeClient(reply(text("Nothing today.")))
        self._run(client)
        self.assertEqual(len(client.calls[0]["messages"]), 2)
        self.assertEqual(AgentJob.objects.get().status, AgentJob.Status.DONE)

    def test_permanent_failure_tells_the_user(self):
        self._queue(28, "Hello")
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        bad = anthropic.BadRequestError("bad", response=httpx2.Response(400, request=request), body=None)
        self._run(FakeClient(bad))

        self.assertEqual(AgentJob.objects.get().status, AgentJob.Status.FAILED)
        self.assertIn("something went wrong", self.bot.messages[-1][1])

    def test_interrupted_tool_call_is_closed_before_continuing(self):
        self._queue(29, "Tasks?")
        job = claim_next_jobs()[0]
        thread = AgentThread.objects.create(
            channel="telegram", role="ike", telegram_chat=job.telegram_chat,
            telegram_user=job.telegram_user, last_activity_at=timezone.now(),
        )
        AgentMessage.objects.create(thread=thread, role="user", content=[text("Tasks?")])
        AgentMessage.objects.create(thread=thread, role="system", content="context")
        AgentMessage.objects.create(
            thread=thread, role="assistant", content=[tool_use("list_tasks", {"assigned_to": "any", "include_completed": False})]
        )
        AgentJob.objects.filter(id=job.id).update(thread=thread)
        job.refresh_from_db()

        client = FakeClient(reply(text("Here you go.")))
        run_jobs([job], bot=self.bot, client=client)

        last_sent = client.calls[0]["messages"][-1]
        self.assertEqual(last_sent["content"][0]["content"], INTERRUPTED_RESULT)
        self.assertTrue(last_sent["content"][0]["is_error"])

    def test_orphaned_running_jobs_are_requeued(self):
        self._queue(30, "Hi")
        claim_next_jobs()
        self.assertEqual(requeue_orphaned_jobs(), 1)
        self.assertEqual(AgentJob.objects.get().status, AgentJob.Status.QUEUED)

    @override_settings(AGENT_MAX_TOOL_ROUNDS=1)
    def test_tool_budget_forces_a_final_answer(self):
        self._queue(31, "Notes?")
        client = FakeClient(
            reply(tool_use("search_notes", {"query": ""}), stop_reason="tool_use"),
            reply(text("No notes yet.")),
        )
        self._run(client)
        self.assertNotIn("tool_choice", client.calls[0])
        self.assertEqual(client.calls[1]["tool_choice"], {"type": "none"})

    def test_refusal_gets_a_plain_reply(self):
        self._queue(32, "something")
        self._run(FakeClient(reply(text(""), stop_reason="refusal")))
        self.assertEqual(self.bot.messages[-1][1], REFUSAL_REPLY)

    def test_bad_tool_input_is_reported_back_to_the_model(self):
        self._queue(33, "calendar")
        client = FakeClient(
            reply(tool_use("get_calendar", {"start_date": "soon", "end_date": "2026-10-20", "search": ""}), stop_reason="tool_use"),
            reply(text("Which dates?")),
        )
        self._run(client)
        result = client.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("YYYY-MM-DD", result["content"])

    def test_remembered_facts_appear_in_the_next_conversation(self):
        self._queue(34, "My GP is Dr Patel")
        self._run(FakeClient(
            reply(tool_use("remember", {"text": "GP is Dr Patel"}), stop_reason="tool_use"),
            reply(text("Noted.")),
        ))
        memory = AgentMemory.objects.get()
        self.assertEqual((memory.role, memory.text), ("ike", "GP is Dr Patel"))

        AgentThread.objects.update(closed_at=timezone.now())
        self._queue(35, "Who's my GP?")
        client = FakeClient(reply(text("Dr Patel.")))
        self._run(client)
        self.assertIn(f"[{memory.id}] GP is Dr Patel", client.calls[0]["messages"][1]["content"])

    def test_forget_only_touches_the_speakers_memories(self):
        wife_memory = AgentMemory.objects.create(role="wife", text="Prefers mornings")
        self._queue(36, "forget that")
        client = FakeClient(
            reply(tool_use("forget", {"memory_id": wife_memory.id}), stop_reason="tool_use"),
            reply(text("Couldn't find that.")),
        )
        self._run(client)
        wife_memory.refresh_from_db()
        self.assertTrue(wife_memory.active)
        self.assertTrue(client.calls[1]["messages"][-1]["content"][0]["is_error"])

    @patch("assistant.agent.tools.list_calendar_events", side_effect=RuntimeError("no creds"))
    def test_calendar_falls_back_to_lifeos_records(self, _list_events):
        self._queue(40, "This week?")
        client = FakeClient(
            reply(tool_use("get_calendar", {"start_date": "2026-10-15", "end_date": "2026-10-21", "search": ""}), stop_reason="tool_use"),
            reply(text("Quiet week.")),
        )
        self._run(client)
        payload = json.loads(client.calls[1]["messages"][-1]["content"][0]["content"])
        self.assertEqual(payload["source"], "lifeos_records")


class FormattingTests(TestCase):
    def test_escapes_html_and_keeps_bold(self):
        self.assertEqual(
            to_telegram_html("## Plan\n**Fri** 5 < 6 & <b>"),
            "Plan\n<b>Fri</b> 5 &lt; 6 &amp; &lt;b&gt;",
        )

    def test_long_text_splits_on_paragraphs(self):
        chunks = split_message("a" * 30 + "\n\n" + "b" * 30, limit=40)
        self.assertEqual(chunks, ["a" * 30, "b" * 30])
