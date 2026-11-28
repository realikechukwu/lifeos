"""Tests for the LifeOS web agent ("Ask") channel, endpoints, UI and formatter.

Zero live network calls: Anthropic, Google Calendar, Gmail and Telegram are
all faked using the helpers from assistant.tests.test_agent.
"""

from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

import anthropic
from docx import Document
import httpx2
from pypdf import PdfWriter
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from assistant.agent.documents import DOCX_MIME, MAX_UPLOAD_BYTES
from assistant.agent.formatting import to_web_html
from assistant.agent.loop import PROMPT_VERSION
from assistant.agent.photos import MAX_PHOTO_BYTES
from assistant.agent.proposals import ToolInputError
from assistant.agent.web import close_thread, enqueue_web_message, thread_for
from assistant.agent.worker import claim_next_jobs, run_jobs
from assistant.services.telegram_handlers import process_telegram_update
from assistant.tests.test_agent import (
    AGENT_SETTINGS,
    AgentFakeBot,
    FakeClient,
    reply,
    server_error,
    text,
    tool_use,
)
from assistant.tests.test_telegram import message_update
from core.models import (
    AgentJob,
    AgentMemory,
    AgentMessage,
    AgentOutcome,
    AgentProposal,
    AgentThread,
    AgentUpload,
    AuditLog,
    GeneratedDocument,
    HouseholdMember,
    Task,
    WebAccount,
)

User = get_user_model()
FRAGMENT = {"HTTP_X_FRAGMENT": "1"}
WEB_ASK_SETTINGS = {
    **AGENT_SETTINGS,
    "AGENT_WEB_CHAT_ENABLED": True,
    "AGENT_PROPOSALS_ENABLED": True,
    "AGENT_DOCUMENTS_ENABLED": True,
    "AGENT_PHOTOS_ENABLED": True,
    "AGENT_PERSONAL_INBOX_ENABLED": True,
    "PERSONAL_GMAIL_IKE": "ike@example.com",
}


def _minimal_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _minimal_docx_bytes(paragraph: str) -> bytes:
    doc = Document()
    doc.add_paragraph(paragraph)
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


class WebAskFlagOffTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ike", password="pw123456!")
        WebAccount.objects.create(user=self.user, role=HouseholdMember.Role.IKE)
        self.client.force_login(self.user)

    @override_settings(AGENT_WEB_CHAT_ENABLED=False)
    def test_flag_off_returns_404_for_all_ask_urls_and_keeps_new_button(self):
        for method, url in [
            ("get", reverse("web:ask")),
            ("post", reverse("web:ask_send")),
            ("get", reverse("web:ask_updates")),
            ("post", reverse("web:ask_new")),
            ("post", reverse("web:ask_retry", args=[1])),
            ("post", reverse("web:ask_proposal_confirm", args=[1])),
            ("post", reverse("web:ask_proposal_cancel", args=[1])),
            ("post", reverse("web:ask_proposals_confirm_all")),
            ("get", reverse("web:ask_document_download", args=[1])),
        ]:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 404)

        home = self.client.get(reverse("web:upcoming"))
        self.assertContains(home, 'data-open-dialog="quickAdd"')
        self.assertNotContains(home, f'href="{reverse("web:ask")}"')
        tasks_page = self.client.get(reverse("web:task_list"))
        self.assertNotContains(tasks_page, 'aria-label="New task or note"')

    @override_settings(AGENT_WEB_CHAT_ENABLED=True, AGENT_ENABLED=False)
    def test_agent_disabled_makes_ask_unavailable_and_refuses_send(self):
        home = self.client.get(reverse("web:upcoming"))
        self.assertFalse(home.context["ask_available"])
        self.assertNotContains(home, f'href="{reverse("web:ask")}"')

        response = self.client.post(reverse("web:ask_send"), {"message": "Hi"}, **FRAGMENT)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AgentJob.objects.exists())

        account = WebAccount.objects.get(user=self.user)
        with self.assertRaises(ToolInputError):
            enqueue_web_message(user=self.user, account=account, text="Hi")


@override_settings(**WEB_ASK_SETTINGS)
class WebAskAccountSetupTests(TestCase):
    def setUp(self):
        self.unlinked = User.objects.create_user(username="guest", password="pw123456!")
        self.ike = User.objects.create_user(username="ike", password="pw123456!")
        WebAccount.objects.create(user=self.ike, role=HouseholdMember.Role.IKE)

    def test_user_without_web_account_sees_friendly_not_setup_page_and_keeps_new_button(self):
        self.client.force_login(self.unlinked)
        response = self.client.get(reverse("web:ask"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "web/ask_not_setup.html")
        self.assertContains(response, "Ask isn’t set up for your account yet")

        home = self.client.get(reverse("web:upcoming"))
        self.assertNotContains(home, f'href="{reverse("web:ask")}"')

        self.assertEqual(self.client.post(reverse("web:ask_send"), {"message": "Hi"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("web:ask_updates")).status_code, 403)
        self.assertEqual(self.client.post(reverse("web:ask_new")).status_code, 403)

    def test_linked_user_gets_ask_center_button_and_tasks_quick_add(self):
        self.client.force_login(self.ike)
        home = self.client.get(reverse("web:upcoming"))
        self.assertContains(home, f'href="{reverse("web:ask")}"')
        tasks_page = self.client.get(reverse("web:task_list"))
        self.assertContains(tasks_page, 'aria-label="New task or note"')


@override_settings(**WEB_ASK_SETTINGS)
class WebAskThreadAndChannelTests(TestCase):
    def setUp(self):
        self.ike = User.objects.create_user(username="ike", password="pw123456!")
        self.wife = User.objects.create_user(username="wife", first_name="Ada", password="pw123456!")
        self.ike_account = WebAccount.objects.create(user=self.ike, role=HouseholdMember.Role.IKE)
        self.wife_account = WebAccount.objects.create(user=self.wife, role=HouseholdMember.Role.WIFE)

    def _run_worker(self, client, bot=None):
        jobs = claim_next_jobs()
        self.assertTrue(jobs)
        run_jobs(jobs, bot=bot, client=client)
        return jobs

    def test_each_user_gets_own_thread_and_threads_rotate_on_idle_prompt_and_new_chat(self):
        ike_thread = thread_for(self.ike, self.ike_account.role)
        wife_thread = thread_for(self.wife, self.wife_account.role)
        self.assertNotEqual(ike_thread.id, wife_thread.id)
        self.assertEqual(thread_for(self.ike, self.ike_account.role).id, ike_thread.id)

        # Idle rotation
        AgentThread.objects.filter(pk=ike_thread.pk).update(
            last_activity_at=timezone.now() - timedelta(hours=9)
        )
        rotated_idle = thread_for(self.ike, self.ike_account.role)
        self.assertNotEqual(rotated_idle.id, ike_thread.id)
        ike_thread.refresh_from_db()
        self.assertIsNotNone(ike_thread.closed_at)

        # PROMPT_VERSION rotation
        AgentThread.objects.filter(pk=rotated_idle.pk).update(prompt_version="stale-version")
        rotated_prompt = thread_for(self.ike, self.ike_account.role)
        self.assertNotEqual(rotated_prompt.id, rotated_idle.id)
        self.assertEqual(rotated_prompt.prompt_version, PROMPT_VERSION)
        rotated_idle.refresh_from_db()
        self.assertIsNotNone(rotated_idle.closed_at)

        # New chat closes thread
        self.client.force_login(self.ike)
        response = self.client.post(reverse("web:ask_new"), **FRAGMENT)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ask about your plans")
        rotated_prompt.refresh_from_db()
        self.assertIsNotNone(rotated_prompt.closed_at)
        self.assertFalse(close_thread(self.ike))

    def test_send_worker_updates_context_note_and_cache_invariants(self):
        AgentMemory.objects.create(role=HouseholdMember.Role.IKE, text="Prefers 07:30 reminders")
        self.client.force_login(self.ike)

        send_resp = self.client.post(
            reverse("web:ask_send"), {"message": "What's on this week?"}, **FRAGMENT
        )
        self.assertEqual(send_resp.status_code, 200)
        self.assertContains(send_resp, "What&#x27;s on this week?")
        self.assertContains(send_resp, "data-ask-typing")

        job = AgentJob.objects.get()
        self.assertEqual(job.channel, AgentThread.Channel.WEB)
        self.assertEqual(job.status, AgentJob.Status.QUEUED)
        self.assertEqual(job.web_user, self.ike)

        # While queued, updates endpoint keeps polling
        poll_before = self.client.get(reverse("web:ask_updates"), {"after": 0}).json()
        self.assertTrue(poll_before["polling"])
        self.assertEqual(poll_before["html"], "")

        web_client = FakeClient(reply(text("You have **one** event:\n• Fri: Dentist")))
        self._run_worker(web_client)

        job.refresh_from_db()
        self.assertEqual(job.status, AgentJob.Status.DONE)
        self.assertEqual(job.text, "")

        # Verify context note and message ordering
        sent_messages = web_client.calls[0]["messages"]
        self.assertEqual([m["role"] for m in sent_messages], ["user", "system"])
        context = sent_messages[1]["content"]
        self.assertIn("private web chat", context)
        self.assertIn("Speaker: Ike", context)
        self.assertIn("Europe/London", context)
        self.assertIn("Prefers 07:30 reminders", context)

        # Updates endpoint returns formatted assistant reply (without duplicating user bubble) and stops polling
        poll_after = self.client.get(reverse("web:ask_updates"), {"after": 0}).json()
        self.assertFalse(poll_after["polling"])
        self.assertNotIn("ask-turn-user", poll_after["html"])
        self.assertIn("<strong>one</strong>", poll_after["html"])
        self.assertIn("<li>Fri: Dentist</li>", poll_after["html"])
        self.assertGreater(poll_after["last_message_id"], 0)

        # Second turn in web chat + a Telegram turn in the mix keep system & tools identical
        self.client.post(reverse("web:ask_send"), {"message": "And Saturday?"}, **FRAGMENT)
        web_client_2 = FakeClient(reply(text("Saturday is free.")))
        self._run_worker(web_client_2)

        bot = AgentFakeBot()
        process_telegram_update(message_update(501, text="Hi from Telegram"), bot=bot)
        tg_client = FakeClient(reply(text("Hi back!")))
        self._run_worker(tg_client, bot=bot)

        self.assertEqual(web_client.calls[0]["system"], web_client_2.calls[0]["system"])
        self.assertEqual(web_client.calls[0]["tools"], web_client_2.calls[0]["tools"])
        self.assertEqual(web_client.calls[0]["system"], tg_client.calls[0]["system"])
        self.assertEqual(web_client.calls[0]["tools"], tg_client.calls[0]["tools"])
        # Second turn does not repeat saved memories
        self.assertNotIn("Prefers 07:30 reminders", web_client_2.calls[0]["messages"][-1]["content"])

    @patch("assistant.agent.tools.personal_inbox.search_messages", return_value=[{"id": "m1", "subject": "School"}])
    def test_private_web_chat_allows_personal_inbox_and_refuses_email_only_confirm_proposal(self, mock_search):
        self.client.force_login(self.ike)
        self.client.post(reverse("web:ask_send"), {"message": "Check my inbox"}, **FRAGMENT)
        client = FakeClient(
            reply(
                tool_use("search_my_inbox", {"query": "school", "max_results": 5}, tool_id="toolu_inbox"),
                tool_use("confirm_proposal", {"proposal_id": 1}, tool_id="toolu_confirm"),
                stop_reason="tool_use",
            ),
            reply(text("Found one school email.")),
        )
        self._run_worker(client)

        mock_search.assert_called_once_with("ike@example.com", "school", max_results=5)
        results = client.calls[1]["messages"][-1]["content"]
        self.assertFalse(results[0].get("is_error"))
        self.assertIn("School", results[0]["content"])
        self.assertTrue(results[1].get("is_error"))
        self.assertIn("email channel", results[1]["content"])

    def test_transient_retry_and_permanent_failure_never_messages_telegram(self):
        self.client.force_login(self.ike)
        self.client.post(reverse("web:ask_send"), {"message": "Hello"}, **FRAGMENT)
        bot = AgentFakeBot()

        # Transient error re-queues without repeating input
        self._run_worker(FakeClient(server_error()), bot=bot)
        job = AgentJob.objects.get()
        self.assertEqual(job.status, AgentJob.Status.QUEUED)
        self.assertIsNotNone(job.thread_id)
        self.assertEqual(bot.messages, [])

        # Permanent error marks FAILED, does not message Telegram, UI shows Retry
        AgentJob.objects.update(started_at=timezone.now() - timedelta(minutes=1))
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        bad = anthropic.BadRequestError("bad", response=httpx2.Response(400, request=request), body=None)
        self._run_worker(FakeClient(bad), bot=bot)

        job.refresh_from_db()
        self.assertEqual(job.status, AgentJob.Status.FAILED)
        self.assertEqual(bot.messages, [])

        poll_failed = self.client.get(reverse("web:ask_updates"), {"after": 0}).json()
        self.assertFalse(poll_failed["polling"])
        self.assertTrue(poll_failed["failed"])
        self.assertIn("Something went wrong", poll_failed["html"])
        self.assertIn(reverse("web:ask_retry", args=[job.id]), poll_failed["html"])

        # Clicking Retry requeues the same job and succeeds without duplicating the user message
        retry_resp = self.client.post(reverse("web:ask_retry", args=[job.id]), **FRAGMENT)
        self.assertEqual(retry_resp.status_code, 200)
        self.assertContains(retry_resp, "data-ask-typing")

        retry_client = FakeClient(reply(text("All good now.")))
        self._run_worker(retry_client, bot=bot)
        self.assertEqual(len(retry_client.calls[0]["messages"]), 2)
        self.assertEqual(AgentJob.objects.get().status, AgentJob.Status.DONE)

    def test_retry_refuses_stale_prompt_version_and_idle_thread_with_409(self):
        self.client.force_login(self.ike)

        # Case 1: prompt_version rotated after failure
        self.client.post(reverse("web:ask_send"), {"message": "First try"}, **FRAGMENT)
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        bad = anthropic.BadRequestError("bad", response=httpx2.Response(400, request=request), body=None)
        self._run_worker(FakeClient(bad))
        job = AgentJob.objects.order_by("id").last()
        self.assertEqual(job.status, AgentJob.Status.FAILED)

        AgentThread.objects.filter(pk=job.thread_id).update(prompt_version="stale-version")
        resp = self.client.post(reverse("web:ask_retry", args=[job.id]), **FRAGMENT)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("send your message again", resp.content.decode().lower())
        job.refresh_from_db()
        self.assertEqual(job.status, AgentJob.Status.FAILED)
        self.assertIsNotNone(job.thread.closed_at)

        # Case 2: thread past idle limit after failure
        self.client.post(reverse("web:ask_send"), {"message": "Second try"}, **FRAGMENT)
        self._run_worker(FakeClient(bad))
        job2 = AgentJob.objects.order_by("id").last()
        AgentThread.objects.filter(pk=job2.thread_id).update(
            last_activity_at=timezone.now() - timedelta(hours=9)
        )
        resp2 = self.client.post(reverse("web:ask_retry", args=[job2.id]))
        self.assertEqual(resp2.status_code, 409)
        self.assertIn("send your message again", resp2.content.decode().lower())
        job2.refresh_from_db()
        self.assertEqual(job2.status, AgentJob.Status.FAILED)
        self.assertIsNotNone(job2.thread.closed_at)

    def test_updates_poll_does_not_load_earlier_upload_messages_and_uses_job_metadata(self):
        self.client.force_login(self.ike)

        # Turn 1: earlier message containing an uploaded photo
        img = SimpleUploadedFile("receipt.png", b"\x89PNG\r\n\x1a\n" + b"x" * 200, content_type="image/png")
        self.assertEqual(
            self.client.post(reverse("web:ask_send"), {"message": "Look at this", "attachment": img}, **FRAGMENT).status_code,
            200,
        )
        self._run_worker(FakeClient(reply(text("Got your receipt."))))
        upload_msg_id = AgentMessage.objects.filter(role=AgentMessage.Role.USER).order_by("id").first().id
        after_turn_1 = AgentMessage.objects.order_by("-id").values_list("id", flat=True).first()

        # Turn 2 queued: poll should never select message content/content_json or earlier rows
        self.client.post(reverse("web:ask_send"), {"message": "Add a task and draft a doc"}, **FRAGMENT)
        with CaptureQueriesContext(connection) as queued_queries:
            poll_queued = self.client.get(reverse("web:ask_updates"), {"after": after_turn_1}).json()
        self.assertTrue(poll_queued["polling"])
        self.assertEqual(poll_queued["html"], "")
        msg_sqls = [q["sql"] for q in queued_queries if "core_agentmessage" in q["sql"]]
        self.assertTrue(msg_sqls)
        for sql in msg_sqls:
            self.assertIn(f"> {after_turn_1}", sql)
            self.assertNotIn('"content"', sql)
            self.assertNotIn('"content_json"', sql)

        # Worker finishes Turn 2 with a proposal and a generated document
        self._run_worker(FakeClient(
            reply(
                tool_use("propose_task", {"title": "Pay invoice"}, tool_id="toolu_p"),
                tool_use("make_document", {"title": "Summary", "content": "Details"}, tool_id="toolu_d"),
                stop_reason="tool_use",
            ),
            reply(text("Done — ready to confirm.")),
        ))
        reply_msg_id = AgentMessage.objects.filter(role=AgentMessage.Role.ASSISTANT).order_by("-id").first().id

        # Poll after Turn 2: must query only id > after_turn_1 / new reply ids, never earlier upload message
        with CaptureQueriesContext(connection) as done_queries:
            poll_done = self.client.get(reverse("web:ask_updates"), {"after": after_turn_1}).json()
        self.assertFalse(poll_done["polling"])
        self.assertIn("Done — ready to confirm.", poll_done["html"])
        self.assertIn("Pay invoice", poll_done["html"])
        self.assertIn("summary.docx", poll_done["html"])
        self.assertEqual(poll_done["last_message_id"], reply_msg_id)

        done_msg_sqls = [q["sql"] for q in done_queries if "core_agentmessage" in q["sql"]]
        self.assertEqual(len(done_msg_sqls), 2, done_msg_sqls)
        scan_sql, fetch_sql = done_msg_sqls
        self.assertIn(f'"core_agentmessage"."id" > {after_turn_1}', scan_sql)
        self.assertNotIn('"content"', scan_sql)
        self.assertNotIn('"content_json"', scan_sql)
        self.assertIn(f'"core_agentmessage"."id" IN ({reply_msg_id})', fetch_sql)
        self.assertGreater(reply_msg_id, after_turn_1)
        self.assertLess(upload_msg_id, after_turn_1)
        doc_sqls = [q["sql"] for q in done_queries if "core_generateddocument" in q["sql"]]
        self.assertTrue(doc_sqls)
        for sql in doc_sqls:
            self.assertNotIn('"data"', sql)


@override_settings(**WEB_ASK_SETTINGS)
class WebAskProposalTests(TestCase):
    def setUp(self):
        self.ike = User.objects.create_user(username="ike", password="pw123456!")
        self.wife = User.objects.create_user(username="wife", first_name="Ada", password="pw123456!")
        WebAccount.objects.create(user=self.ike, role=HouseholdMember.Role.IKE)
        WebAccount.objects.create(user=self.wife, role=HouseholdMember.Role.WIFE)

    def _create_proposed_task(self, title="Buy milk"):
        self.client.force_login(self.ike)
        self.client.post(reverse("web:ask_send"), {"message": f"Add task {title}"}, **FRAGMENT)
        client = FakeClient(
            reply(tool_use("propose_task", {"title": title, "assigned_to": "ike"}), stop_reason="tool_use"),
            reply(text("Ready to add that.")),
        )
        jobs = claim_next_jobs()
        run_jobs(jobs, client=client)
        return AgentProposal.objects.order_by("id").last()

    def test_proposal_confirm_cancel_permissions_and_next_turn_outcome(self):
        proposal = self._create_proposed_task("Book MOT")

        # GET is rejected
        self.assertEqual(self.client.get(reverse("web:ask_proposal_confirm", args=[proposal.id])).status_code, 405)
        self.assertEqual(self.client.get(reverse("web:ask_proposal_cancel", args=[proposal.id])).status_code, 405)

        # Another role is rejected
        self.client.force_login(self.wife)
        self.assertEqual(self.client.post(reverse("web:ask_proposal_confirm", args=[proposal.id])).status_code, 404)
        self.assertEqual(self.client.post(reverse("web:ask_proposal_cancel", args=[proposal.id])).status_code, 404)

        # Owner confirms
        self.client.force_login(self.ike)
        resp = self.client.post(reverse("web:ask_proposal_confirm", args=[proposal.id]), **FRAGMENT)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Added to Tasks")
        self.assertTrue(Task.objects.filter(title="Book MOT").exists())
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, AgentProposal.Status.CONFIRMED)
        self.assertTrue(AgentOutcome.objects.filter(thread=proposal.thread, delivered_at__isnull=True).exists())

        # Non-pending proposal is rejected
        self.assertEqual(self.client.post(reverse("web:ask_proposal_confirm", args=[proposal.id])).status_code, 409)
        self.assertEqual(self.client.post(reverse("web:ask_proposal_cancel", args=[proposal.id])).status_code, 409)

        # Outcome is delivered on the next turn's context note
        self.client.post(reverse("web:ask_send"), {"message": "Thanks"}, **FRAGMENT)
        next_client = FakeClient(reply(text("You're welcome!")))
        run_jobs(claim_next_jobs(), client=next_client)
        next_context = next_client.calls[0]["messages"][-1]["content"]
        self.assertIn("Since your last reply:", next_context)
        self.assertIn("Book MOT", next_context)
        self.assertFalse(AgentOutcome.objects.filter(thread=proposal.thread, delivered_at__isnull=True).exists())

        # Cancel flow
        proposal2 = self._create_proposed_task("Skip this")
        cancel_resp = self.client.post(reverse("web:ask_proposal_cancel", args=[proposal2.id]), **FRAGMENT)
        self.assertEqual(cancel_resp.status_code, 200)
        self.assertContains(cancel_resp, "Cancelled")
        proposal2.refresh_from_db()
        self.assertEqual(proposal2.status, AgentProposal.Status.CANCELLED)
        self.assertFalse(Task.objects.filter(title="Skip this").exists())

    def test_multiple_pending_proposals_render_add_all_and_confirm_all(self):
        self.client.force_login(self.ike)
        self.client.post(reverse("web:ask_send"), {"message": "Add two tasks"}, **FRAGMENT)
        client = FakeClient(
            reply(
                tool_use("propose_task", {"title": "Task One"}, tool_id="toolu_a"),
                tool_use("propose_task", {"title": "Task Two"}, tool_id="toolu_b"),
                stop_reason="tool_use",
            ),
            reply(text("Both are ready.")),
        )
        run_jobs(claim_next_jobs(), client=client)

        page = self.client.get(reverse("web:ask"))
        self.assertContains(page, "Add all")
        ids = list(AgentProposal.objects.order_by("id").values_list("id", flat=True))
        resp = self.client.post(
            reverse("web:ask_proposals_confirm_all"),
            {"proposal_id": ids, "group_id": "proposals-test"},
            **FRAGMENT,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Add all")
        self.assertEqual(Task.objects.filter(title__in=["Task One", "Task Two"]).count(), 2)


@override_settings(**WEB_ASK_SETTINGS)
class WebAskUploadAndDocumentTests(TestCase):
    def setUp(self):
        self.ike = User.objects.create_user(username="ike", password="pw123456!")
        self.wife = User.objects.create_user(username="wife", password="pw123456!")
        WebAccount.objects.create(user=self.ike, role=HouseholdMember.Role.IKE)
        WebAccount.objects.create(user=self.wife, role=HouseholdMember.Role.WIFE)
        self.client.force_login(self.ike)

    def test_pdf_image_docx_and_markdown_uploads_build_matching_blocks(self):
        # PDF -> document base64 block + AgentUpload
        pdf = SimpleUploadedFile("letter.pdf", _minimal_pdf_bytes(), content_type="application/pdf")
        resp = self.client.post(reverse("web:ask_send"), {"message": "Read this", "attachment": pdf}, **FRAGMENT)
        self.assertEqual(resp.status_code, 200)
        pdf_job = AgentJob.objects.order_by("id").last()
        self.assertEqual(pdf_job.input_blocks[1]["text"], "Untrusted uploaded document: letter.pdf")
        self.assertEqual(pdf_job.input_blocks[2]["type"], "document")
        self.assertEqual(pdf_job.input_blocks[2]["source"]["media_type"], "application/pdf")
        self.assertTrue(AgentUpload.objects.filter(job=pdf_job, filename="letter.pdf").exists())

        # Image -> image base64 block + AuditLog
        img = SimpleUploadedFile("card.png", b"\x89PNG\r\n\x1a\nfake", content_type="image/png")
        resp = self.client.post(reverse("web:ask_send"), {"attachment": img}, **FRAGMENT)
        self.assertEqual(resp.status_code, 200)
        img_job = AgentJob.objects.order_by("id").last()
        self.assertEqual(img_job.input_blocks[1]["text"], "Untrusted photo sent by the speaker:")
        self.assertEqual(img_job.input_blocks[2]["type"], "image")
        self.assertEqual(img_job.input_blocks[2]["source"]["media_type"], "image/png")
        self.assertTrue(AuditLog.objects.filter(action="agent_photo_received", object_id=str(img_job.pk)).exists())

        # DOCX -> extracted text block
        docx = SimpleUploadedFile("cv.docx", _minimal_docx_bytes("Senior Nurse"), content_type=DOCX_MIME)
        resp = self.client.post(reverse("web:ask_send"), {"message": "Tailor", "attachment": docx}, **FRAGMENT)
        self.assertEqual(resp.status_code, 200)
        docx_job = AgentJob.objects.order_by("id").last()
        self.assertEqual(docx_job.input_blocks[2]["type"], "text")
        self.assertIn("Senior Nurse", docx_job.input_blocks[2]["text"])

        # Markdown -> extracted text block
        md = SimpleUploadedFile("notes.md", b"# Skills\nDjango", content_type="application/octet-stream")
        resp = self.client.post(reverse("web:ask_send"), {"attachment": md}, **FRAGMENT)
        self.assertEqual(resp.status_code, 200)
        md_job = AgentJob.objects.order_by("id").last()
        self.assertIn("Django", md_job.input_blocks[2]["text"])

    def test_upload_rejects_unsupported_type_and_oversized_files(self):
        bad = SimpleUploadedFile("app.exe", b"MZ...", content_type="application/octet-stream")
        resp = self.client.post(reverse("web:ask_send"), {"attachment": bad}, **FRAGMENT)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(AgentJob.objects.exists())

        huge_img = SimpleUploadedFile("big.jpg", b"x" * (MAX_PHOTO_BYTES + 1), content_type="image/jpeg")
        resp = self.client.post(reverse("web:ask_send"), {"attachment": huge_img}, **FRAGMENT)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(AgentJob.objects.exists())

        huge_txt = SimpleUploadedFile("big.txt", b"a" * (MAX_UPLOAD_BYTES + 1), content_type="text/plain")
        resp = self.client.post(reverse("web:ask_send"), {"attachment": huge_txt}, **FRAGMENT)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(AgentJob.objects.exists())

    @override_settings(AGENT_PHOTOS_ENABLED=False)
    def test_photo_upload_refused_when_photos_flag_is_off(self):
        img = SimpleUploadedFile("card.png", b"\x89PNG\r\n\x1a\nfake", content_type="image/png")
        resp = self.client.post(reverse("web:ask_send"), {"attachment": img}, **FRAGMENT)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(AgentJob.objects.exists())

        # Plain text and documents are unaffected when only photos are disabled
        ok_resp = self.client.post(reverse("web:ask_send"), {"message": "Just text"}, **FRAGMENT)
        self.assertEqual(ok_resp.status_code, 200)
        self.assertEqual(AgentJob.objects.count(), 1)

    @override_settings(AGENT_DOCUMENTS_ENABLED=False)
    def test_document_upload_refused_when_documents_flag_is_off(self):
        pdf = SimpleUploadedFile("letter.pdf", _minimal_pdf_bytes(), content_type="application/pdf")
        resp = self.client.post(reverse("web:ask_send"), {"attachment": pdf}, **FRAGMENT)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(AgentJob.objects.exists())
        self.assertFalse(AgentUpload.objects.exists())

        md = SimpleUploadedFile("notes.md", b"# Skills", content_type="text/markdown")
        resp_md = self.client.post(reverse("web:ask_send"), {"attachment": md}, **FRAGMENT)
        self.assertEqual(resp_md.status_code, 400)
        self.assertFalse(AgentJob.objects.exists())

        # Photos remain allowed when only documents are disabled
        img = SimpleUploadedFile("card.png", b"\x89PNG\r\n\x1a\nfake", content_type="image/png")
        ok_resp = self.client.post(reverse("web:ask_send"), {"attachment": img}, **FRAGMENT)
        self.assertEqual(ok_resp.status_code, 200)
        self.assertEqual(AgentJob.objects.count(), 1)

    def test_generated_document_download_link_and_role_check(self):
        self.client.post(reverse("web:ask_send"), {"message": "Write a cover letter"}, **FRAGMENT)
        client = FakeClient(
            reply(
                tool_use("make_document", {"title": "Cover Letter", "content": "# Intro\nHello **world**"}),
                stop_reason="tool_use",
            ),
            reply(text("Here is your cover letter.")),
        )
        run_jobs(claim_next_jobs(), client=client)

        doc = GeneratedDocument.objects.get()
        download_url = reverse("web:ask_document_download", args=[doc.id])

        page = self.client.get(reverse("web:ask"))
        self.assertContains(page, download_url)
        self.assertContains(page, doc.filename)

        dl = self.client.get(download_url)
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl["Content-Type"], DOCX_MIME)
        self.assertIn(doc.filename, dl["Content-Disposition"])

        # Other role cannot download Ike's document
        self.client.force_login(self.wife)
        self.assertEqual(self.client.get(download_url).status_code, 404)


class WebAssistantFormatterTests(TestCase):
    def test_escapes_html_and_supports_only_bold_and_bullet_lists(self):
        raw = (
            "## Plan\n"
            "You have **2 events** <script>alert(1)</script> & \"quotes\"\n"
            "• **Fri**: Dentist <br>\n"
            "• [link](javascript:alert(1))\n\n"
            "All clear."
        )
        rendered = to_web_html(raw)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<a ", rendered)
        self.assertNotIn("<h2>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertIn("&amp; &quot;quotes&quot;", rendered)
        self.assertIn("<strong>2 events</strong>", rendered)
        self.assertIn('<ul class="ask-list"><li><strong>Fri</strong>: Dentist &lt;br&gt;</li><li>[link](javascript:alert(1))</li></ul>', rendered)
        self.assertIn("<p>All clear.</p>", rendered)
