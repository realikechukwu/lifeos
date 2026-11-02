import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from assistant.agent.proposals import ToolInputError, decide, handle_callback, propose, summary, tool_definitions, validate_payload
from assistant.agent.tools import HANDLERS, ToolContext
from assistant.agent.worker import claim_next_jobs, run_jobs
from assistant.services.telegram_handlers import process_telegram_update
from assistant.tests.test_agent import AGENT_SETTINGS, AgentFakeBot, FakeClient, reply, text, tool_use
from assistant.tests.test_telegram import message_update
from core.models import AgentJob, AgentMessage, AgentProposal, AgentThread, AuditLog, IncomingEmail, Note, Reminder, Task


@override_settings(**AGENT_SETTINGS, AGENT_PROPOSALS_ENABLED=True, GOOGLE_CALENDAR_ID="family")
class ProposalTests(TestCase):
    def setUp(self):
        self.bot = AgentFakeBot()
        self.bot.edit_message = Mock()
        process_telegram_update(message_update(1, text="Add a task"), bot=self.bot)
        job = AgentJob.objects.get()
        self.thread = AgentThread.objects.create(
            channel="telegram", role="ike", telegram_user=job.telegram_user,
            telegram_chat=job.telegram_chat, last_activity_at=timezone.now(),
        )
        self.ctx = ToolContext(role="ike", speaker_name="Ike", thread=self.thread,
                               telegram_user=job.telegram_user, telegram_chat=job.telegram_chat, bot=self.bot)
        self.day = (timezone.localdate() + timedelta(days=7)).isoformat()

    def make(self, kind="create_task", **args):
        data = propose(self.ctx, args or {"title": "Insurance"}, kind=kind)
        return AgentProposal.objects.get(pk=data["proposal_id"])

    def test_strict_validation_rejects_bad_or_past_dates_and_unknown_fields(self):
        for args in [
            {"title": "A", "due_date": "soon"}, {"title": "A", "due_date": "2000-01-01"},
            {"title": "A", "recipient": "attacker@example.com"}, {"title": 123},
            {"title": "A", "due_time": "10:00"},
        ]:
            with self.subTest(args=args), self.assertRaises(ToolInputError):
                self.make(**args)
        self.assertEqual(AgentProposal.objects.count(), 0)

    def test_event_validation_and_recurrence_do_not_silently_drop_errors(self):
        base = {"title": "Dentist", "appointment_date": self.day, "start_time": "16:00"}
        for extra in [{"end_time": "15:00"}, {"recurrence_until": "2000-01-01"},
                      {"recurrence_frequency": "daily", "recurrence_days_of_week": ["MO"]},
                      {"recurrence_frequency": "weekly", "recurrence_count": 2, "recurrence_until": self.day}]:
            with self.subTest(extra=extra), self.assertRaises(ToolInputError):
                self.make("create_event", **{**base, **extra})

    def test_missing_closed_or_uncancellable_references_rejected(self):
        for kind, args in [("complete_task", {"task_id": 999}), ("cancel_event", {"event_id": 1})]:
            with self.assertRaises(ToolInputError):
                self.make(kind, **args)
        task = Task.objects.create(title="Old", status="completed")
        with self.assertRaises(ToolInputError):
            self.make("update_task", task_id=task.id, title="New")

    def test_confirm_uses_real_pipeline_and_double_tap_is_noop(self):
        p = self.make(title="Insurance", assigned_to="ike")
        self.assertTrue(decide(p, confirm=True))
        self.assertFalse(decide(p, confirm=True))
        self.assertEqual(Task.objects.count(), 1)
        incoming = IncomingEmail.objects.get(gmail_message_id=f"agent-proposal:{p.id}")
        self.assertEqual(incoming.source, "telegram")
        self.assertEqual(Task.objects.get().source_parsed_action.incoming_email, incoming)
        p.refresh_from_db()
        self.assertEqual(p.result["outcome"], "task_created")

    def test_cancel_and_expiry_do_not_execute(self):
        p = self.make()
        decide(p, confirm=False)
        self.assertEqual(AgentProposal.objects.get(pk=p.pk).status, "cancelled")
        p = self.make()
        AgentProposal.objects.filter(pk=p.pk).update(created_at=timezone.now() - timedelta(hours=25))
        p.refresh_from_db()
        decide(p, confirm=True)
        self.assertEqual(AgentProposal.objects.get(pk=p.pk).status, "expired")
        self.assertFalse(Task.objects.exists())

    def test_wrong_user_or_chat_cannot_decide(self):
        p = self.make()
        # Use a real authorised household user from another incoming update.
        process_telegram_update(message_update(2, user_id=1115153420, text="Hi"), bot=self.bot)
        other = AgentJob.objects.get(telegram_update_id=2)
        handle_callback(f"ag:{p.pk}:ok", self.ctx.telegram_chat, other.telegram_user, self.bot)
        p.refresh_from_db()
        self.assertEqual(p.status, "pending")
        self.assertFalse(Task.objects.exists())

    def test_superseding_disables_old_card(self):
        old = self.make()
        old.telegram_message_id = 50
        old.save()
        new = self.make(title="Corrected", replaces_proposal_id=old.id)
        old.refresh_from_db()
        self.assertEqual((old.status, new.replaces_id), ("superseded", old.id))
        self.bot.edit_message.assert_called_once()
        self.assertFalse(decide(old, confirm=True))

    def test_task_edit_completion_and_reminder_cancellation_use_services(self):
        task = Task.objects.create(title="Old", due_date=timezone.localdate() + timedelta(days=3))
        p = self.make("update_task", task_id=task.id, title="New", due_date=self.day, assigned_to="wife")
        decide(p, confirm=True)
        task.refresh_from_db()
        self.assertEqual((task.title, task.assigned_to), ("New", "wife"))
        decide(self.make("complete_task", task_id=task.id), confirm=True)
        task.refresh_from_db()
        self.assertEqual(task.status, "completed")
        reminder = Reminder.objects.create(title="R", recipient="ike", reminder_date=self.day, reminder_time="12:00")
        decide(self.make("cancel_reminder", reminder_id=reminder.id), confirm=True)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "cancelled")

    @patch("assistant.services.google_calendar.get_google_credentials")
    @patch("assistant.services.google_calendar.build")
    def test_event_and_linked_reminder_duplicate_reschedule_and_cancel(self, build, credentials):
        build.return_value.events.return_value.insert.return_value.execute.return_value = {"id": "google1"}
        args = dict(title="Dentist", appointment_date=self.day, start_time="16:00", end_time="17:00",
                    reminder_lead_days=1, reminder_time="16:00", reminder_recipient="ike")
        p = self.make("create_event", **args)
        decide(p, confirm=True)
        p.refresh_from_db()
        self.assertEqual(p.result["outcome"], "created_with_reminder")
        self.assertEqual(Reminder.objects.count(), 1)
        event_id = p.result["objects"]["calendar_event"]
        duplicate = self.make("create_event", **args)
        decide(duplicate, confirm=True)
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.result["outcome"], "duplicate")
        self.assertEqual(Reminder.objects.count(), 1)
        decide(self.make("reschedule_event", event_id=event_id, appointment_date=self.day, start_time="18:00"), confirm=True)
        build.return_value.events.return_value.patch.assert_called_once()
        decide(self.make("cancel_event", event_id=event_id), confirm=True)
        build.return_value.events.return_value.delete.assert_called_once()
        self.assertEqual(Reminder.objects.get().status, "cancelled")

    def test_execution_failure_is_recorded_and_not_retried(self):
        p = self.make()
        with patch("assistant.agent.proposals.router.admin_approve_and_execute", side_effect=RuntimeError("broken")) as execute:
            decide(p, confirm=True)
            decide(p, confirm=True)
        execute.assert_called_once()
        p.refresh_from_db()
        self.assertEqual(p.status, "failed")
        self.assertIn("broken", p.result["detail"])

    def test_worker_sends_reply_before_card_then_outcome_on_next_turn(self):
        client = FakeClient(reply(tool_use("propose_task", {"title": "A < B"}), stop_reason="tool_use"), reply(text("Please confirm.")))
        run_jobs(claim_next_jobs(), bot=self.bot, client=client)
        self.assertEqual(self.bot.messages[0][1], "Please confirm.")
        self.assertIn("A &lt; B", self.bot.messages[1][1])
        p = AgentProposal.objects.get()
        callback = self.bot.messages[1][2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertLess(len(callback.encode()), 64)
        handle_callback(callback, p.telegram_chat, p.telegram_user, self.bot)
        process_telegram_update(message_update(3, text="Did it work?"), bot=self.bot)
        next_client = FakeClient(reply(text("Created.")))
        run_jobs(claim_next_jobs(), bot=self.bot, client=next_client)
        messages = next_client.calls[0]["messages"]
        self.assertEqual(messages[-2]["role"], "user")
        self.assertEqual(messages[-1]["role"], "system")
        self.assertIn("task created", messages[-1]["content"])
        self.assertIn(f"Proposal {p.id}", messages[-1]["content"])
        self.assertEqual(client.calls[0]["system"], next_client.calls[0]["system"])
        self.assertEqual(client.calls[0]["tools"], next_client.calls[0]["tools"])

    @override_settings(AGENT_PROPOSALS_ENABLED=False)
    def test_flag_blocks_proposals(self):
        with self.assertRaises(ToolInputError):
            self.make()


INVENTORY = "Garage\r\nKitchen roll: 6 packs\r\nBin bags: 3 rolls\r\nBin bag ties: 1 pack"


@override_settings(**AGENT_SETTINGS, AGENT_PROPOSALS_ENABLED=True)
class NoteEditTests(TestCase):
    def setUp(self):
        self.bot = AgentFakeBot()
        self.bot.edit_message = Mock()
        process_telegram_update(message_update(1, text="Took 2 kitchen roll"), bot=self.bot)
        job = AgentJob.objects.get()
        self.thread = AgentThread.objects.create(
            channel="telegram", role="ike", telegram_user=job.telegram_user,
            telegram_chat=job.telegram_chat, last_activity_at=timezone.now(),
        )
        self.ctx = ToolContext(role="ike", speaker_name="Ike", thread=self.thread,
                               telegram_user=job.telegram_user, telegram_chat=job.telegram_chat, bot=self.bot)
        self.note = Note.objects.create(title="Garage inventory", body=INVENTORY)

    def make(self, **args):
        data = propose(self.ctx, {"note_id": self.note.id, **args}, kind="update_note")
        return AgentProposal.objects.get(pk=data["proposal_id"])

    def edit(self, find, replace):
        return {"find": find, "replace": replace}

    def test_confirmed_edits_change_only_the_quoted_lines_and_keep_the_old_version(self):
        p = self.make(edits=[self.edit("Kitchen roll: 6 packs", "Kitchen roll: 4 packs"),
                             self.edit("Bin bag ties: 1 pack", "")], append="WD-40: 1 can")
        self.assertEqual(self.note.body, Note.objects.get().body, "nothing changes before Confirm")
        decide(p, confirm=True)
        self.note.refresh_from_db()
        self.assertEqual(self.note.body, "Garage\nKitchen roll: 4 packs\nBin bags: 3 rolls\nWD-40: 1 can")
        log = AuditLog.objects.get(action="update_note")
        self.assertEqual((log.details["previous_body"], log.details["role"]), (INVENTORY, "ike"))

    def test_card_shows_before_and_after(self):
        p = self.make(edits=[self.edit("Kitchen roll: 6 packs", "Kitchen roll: 4 packs"),
                             self.edit("Bin bags: 3 rolls", "")], append="WD-40: 1 can")
        card = summary(p)
        self.assertIn("Update note\nGarage inventory", card)
        self.assertIn("Was: Kitchen roll: 6 packs\nNow: Kitchen roll: 4 packs", card)
        self.assertIn("Was: Bin bags: 3 rolls\nNow: (removed)", card)
        self.assertIn("Add: WD-40: 1 can", card)

    def test_missing_duplicate_or_empty_edits_are_rejected(self):
        for args in [
            {"edits": [self.edit("Kitchen roll: 5 packs", "Kitchen roll: 3 packs")]},
            {"edits": [self.edit("Bin bag", "Sack")]},
            {"edits": [self.edit("", "x")]},
            {"edits": [{"find": "Garage"}]},
            {"edits": [self.edit("Garage", "Garage")]},
            {},
            {"body": "New", "append": "More"},
            {"title": "Renamed"},
        ]:
            with self.subTest(args=args), self.assertRaises(ToolInputError):
                self.make(**args)
        with self.assertRaises(ToolInputError):
            propose(self.ctx, {"note_id": 999, "append": "x"}, kind="update_note")
        self.assertFalse(AgentProposal.objects.exists())

    def test_edit_that_no_longer_matches_at_confirm_fails_without_saving(self):
        p = self.make(edits=[self.edit("Kitchen roll: 6 packs", "Kitchen roll: 4 packs")])
        self.note.body = INVENTORY.replace("6 packs", "5 packs")
        self.note.save()
        decide(p, confirm=True)
        p.refresh_from_db()
        self.assertEqual(p.status, "failed")
        self.assertIn("not in the note", p.result["detail"])
        self.assertIn("5 packs", Note.objects.get().body)

    def test_edit_still_applies_after_an_unrelated_change(self):
        p = self.make(edits=[self.edit("Kitchen roll: 6 packs", "Kitchen roll: 4 packs")])
        self.note.body = INVENTORY + "\r\nPaint: 2 tins"
        self.note.save()
        decide(p, confirm=True)
        self.assertIn("Kitchen roll: 4 packs\nBin bags: 3 rolls\nBin bag ties: 1 pack\nPaint: 2 tins", Note.objects.get().body)

    def test_rewrite_refused_if_the_note_changed_since_it_was_proposed(self):
        p = self.make(body="Garage\nEmpty", new_title="Garage")
        self.assertIn("Whole note replaced", summary(p))
        self.note.body += "\nPaint: 2 tins"
        self.note.save()
        decide(p, confirm=True)
        p.refresh_from_db()
        self.assertEqual(p.status, "failed")
        self.assertIn("Paint", Note.objects.get().body)
        fresh = self.make(body="Garage\nEmpty", new_title="Garage")
        decide(fresh, confirm=True)
        self.assertEqual((Note.objects.get().title, Note.objects.get().body), ("Garage", "Garage\nEmpty"))

    def test_removing_a_whole_line_leaves_no_blank_line(self):
        decide(self.make(edits=[self.edit("Garage", ""), self.edit("Bin bags: 3 rolls", "")]), confirm=True)
        self.assertEqual(Note.objects.get().body, "Kitchen roll: 6 packs\nBin bag ties: 1 pack")
        decide(self.make(edits=[self.edit("ties: 1 pack", "")]), confirm=True)
        self.assertEqual(Note.objects.get().body, "Kitchen roll: 6 packs\nBin bag ")

    def test_append_to_an_empty_list(self):
        self.note.body = ""
        self.note.save()
        decide(self.make(append="Milk\nBread\n"), confirm=True)
        self.assertEqual(Note.objects.get().body, "Milk\nBread")

    def test_read_note_returns_the_full_body_with_plain_newlines(self):
        self.note.body = "Long\r\n" + "x" * 3000
        self.note.save()
        found = json.loads(HANDLERS["search_notes"](self.ctx, {"query": "Long"}))["notes"][0]
        self.assertTrue(found["truncated"])
        full = json.loads(HANDLERS["read_note"](self.ctx, {"note_id": self.note.id}))
        self.assertEqual(full["body"], "Long\n" + "x" * 3000)
        with self.assertRaises(ToolInputError):
            HANDLERS["read_note"](self.ctx, {"note_id": "1"})

    def test_email_note_edits_wait_for_yes(self):
        incoming = IncomingEmail.objects.create(
            gmail_message_id="m1", outer_sender="ike@example.com", subject="Garage",
            body_text="Took 2 kitchen roll", received_at=timezone.now(),
        )
        self.ctx.channel, self.ctx.incoming_email, self.ctx.newest_instruction = "email", incoming, "Took 2 kitchen roll"
        with override_settings(AGENT_EMAIL_ENABLED=True):
            p = self.make(edits=[self.edit("Kitchen roll: 6 packs", "Kitchen roll: 4 packs")])
        self.assertEqual(p.status, "pending")
        self.assertIn("6 packs", Note.objects.get().body)

    def test_tool_schema_lists_note_edit_fields(self):
        definition = next(d for d in tool_definitions() if d["name"] == "propose_update_note")
        properties = definition["input_schema"]["properties"]
        self.assertEqual(set(properties), {"note_id", "edits", "append", "body", "new_title", "replaces_proposal_id"})
        self.assertEqual(properties["edits"]["items"]["required"], ["find", "replace"])
