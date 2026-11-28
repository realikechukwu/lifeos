"""Tests for the iPhone share-sheet endpoint, share keys, the Settings page
and the menu drawer. No live network calls: the agent is faked."""

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from assistant.agent.web import HEIC_MESSAGE, MAX_SHARE_FILES
from assistant.agent.worker import claim_next_jobs, run_jobs
from assistant.tests.test_agent import FakeClient, reply, text, tool_use
from core.models import (
    AgentJob,
    AgentMessage,
    AgentProposal,
    AuditLog,
    HouseholdMember,
    IncomingEmail,
    ParsedAction,
    Task,
    WebAccount,
)

from .test_ask import FRAGMENT, WEB_ASK_SETTINGS, _minimal_pdf_bytes

User = get_user_model()
SHARE_URL = "/ask/share/"


def _png(name="photo.png"):
    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\n" + b"x" * 64, content_type="image/png")


def _pdf(name="letter.pdf"):
    return SimpleUploadedFile(name, _minimal_pdf_bytes(), content_type="application/pdf")


@override_settings(**WEB_ASK_SETTINGS)
class ShareKeyTests(TestCase):
    def setUp(self):
        self.ike = User.objects.create_user(username="ike", password="pw123456!")
        self.account = WebAccount.objects.create(user=self.ike, role=HouseholdMember.Role.IKE)

    def test_key_is_stored_hashed_and_replacing_or_revoking_kills_the_old_one(self):
        raw = self.account.issue_share_key()
        self.account.refresh_from_db()
        self.assertTrue(raw.startswith("lifeos_"))
        self.assertNotIn(raw, self.account.share_key_hash)
        self.assertEqual(self.account.share_key_hash, WebAccount.hash_share_key(raw))
        self.assertEqual(self.account.share_key_hint, raw[-4:])
        self.assertEqual(WebAccount.for_share_key(raw), self.account)
        self.assertEqual(WebAccount.for_share_key(f"  {raw}\n"), self.account)

        newer = self.account.issue_share_key()
        self.assertIsNone(WebAccount.for_share_key(raw))
        self.assertEqual(WebAccount.for_share_key(newer), self.account)

        self.account.revoke_share_key()
        self.assertIsNone(WebAccount.for_share_key(newer))
        self.assertFalse(self.account.has_share_key)

    def test_lookup_refuses_blank_foreign_and_absurd_keys(self):
        self.account.issue_share_key()
        for bad in ["", "   ", "not-a-key", "lifeos_wrong", "lifeos_" + "a" * 500]:
            self.assertIsNone(WebAccount.for_share_key(bad), bad)
        # A revoked account's empty hash must never match anything.
        self.account.revoke_share_key()
        self.assertIsNone(WebAccount.for_share_key("lifeos_"))


@override_settings(**WEB_ASK_SETTINGS)
class ShareEndpointTests(TestCase):
    def setUp(self):
        self.ike = User.objects.create_user(username="ike", password="pw123456!")
        self.wife = User.objects.create_user(username="wife", first_name="Ada", password="pw123456!")
        self.ike_account = WebAccount.objects.create(user=self.ike, role=HouseholdMember.Role.IKE)
        self.wife_account = WebAccount.objects.create(user=self.wife, role=HouseholdMember.Role.WIFE)
        self.key = self.ike_account.issue_share_key()
        # The Shortcut has no session or CSRF token.
        self.phone = Client(enforce_csrf_checks=True)

    def share(self, data=None, key=None, auth_prefix="Bearer "):
        headers = {}
        key = self.key if key is None else key
        if key:
            headers["HTTP_AUTHORIZATION"] = f"{auth_prefix}{key}"
        return self.phone.post(SHARE_URL, data or {}, **headers)

    def test_url_and_method(self):
        self.assertEqual(reverse("web:ask_share"), SHARE_URL)
        self.assertEqual(self.phone.get(SHARE_URL).status_code, 405)

    def test_missing_wrong_revoked_or_inactive_keys_are_refused(self):
        self.assertEqual(self.share({"message": "hi"}, key="").status_code, 401)
        response = self.share({"message": "hi"}, key="lifeos_wrong")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Settings", response.content.decode())
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")

        self.ike.is_active = False
        self.ike.save()
        self.assertEqual(self.share({"message": "hi"}).status_code, 401)
        self.ike.is_active = True
        self.ike.save()

        self.ike_account.revoke_share_key()
        self.assertEqual(self.share({"message": "hi"}).status_code, 401)
        self.assertFalse(AgentJob.objects.exists())

    @override_settings(AGENT_WEB_CHAT_ENABLED=False)
    def test_switched_off_when_web_chat_is_off(self):
        self.assertEqual(self.share({"message": "hi"}).status_code, 503)
        self.assertFalse(AgentJob.objects.exists())

    def test_text_share_queues_a_web_job_without_csrf_and_records_use(self):
        response = self.share({"message": "Add the school trip on Friday"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sent to LifeOS", response.content.decode())

        job = AgentJob.objects.get()
        self.assertEqual(job.channel, "web")
        self.assertEqual(job.web_user, self.ike)
        self.assertEqual(job.text, "Add the school trip on Friday")
        self.assertEqual(job.reply_metadata["source"], "share")
        self.ike_account.refresh_from_db()
        self.assertIsNotNone(self.ike_account.share_key_last_used_at)
        self.assertTrue(AuditLog.objects.filter(action="agent_share_received").exists())

    def test_key_works_with_or_without_the_bearer_prefix(self):
        self.assertEqual(self.share({"message": "one"}, auth_prefix="").status_code, 200)
        self.assertEqual(self.share({"message": "two"}, auth_prefix="bearer ").status_code, 200)
        self.assertEqual(AgentJob.objects.count(), 2)

    def test_shared_text_arriving_as_a_plain_file_field_is_kept_and_labelled(self):
        response = self.share({"message": "Remind me about this", "file": "Parents evening Tue 6pm"})
        self.assertEqual(response.status_code, 200)
        job = AgentJob.objects.get()
        self.assertIn("Remind me about this", job.text)
        self.assertIn("Text shared from another app:\nParents evening Tue 6pm", job.text)

    def test_nothing_shared_is_refused(self):
        response = self.share({"message": "  "})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Nothing was shared", response.content.decode())

    def test_each_key_lands_in_its_own_persons_chat(self):
        wife_key = self.wife_account.issue_share_key()
        self.share({"message": "From Ada"}, key=wife_key)
        self.assertEqual(AgentJob.objects.get().web_user, self.wife)

    def test_several_files_are_answered_as_one_turn_in_ask(self):
        response = self.share({
            "message": "Put these on the calendar",
            "file": [_pdf("letter.pdf"), _png("form.png")],
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("2 files", response.content.decode())
        jobs = list(AgentJob.objects.order_by("id"))
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].text, "Put these on the calendar")
        self.assertEqual(jobs[1].text, "Shared file 2 of 2.")

        # While waiting for the worker, Ask shows one bubble with the
        # instruction and both files, not the second file's placeholder.
        self.client.force_login(self.ike)
        waiting = self.client.get(reverse("web:ask"))
        self.assertContains(waiting, "Put these on the calendar")
        self.assertContains(waiting, "letter.pdf, Photo")
        self.assertNotContains(waiting, "Shared file 2 of 2.")
        self.assertContains(waiting, "ask-turn-user", count=1)

        claimed = claim_next_jobs()
        self.assertEqual([j.id for j in claimed], [j.id for j in jobs])
        fake = FakeClient(reply(text("I found two dates.")))
        run_jobs(claimed, client=fake)

        user_message = fake.calls[0]["messages"][0]
        types = [b["type"] for b in user_message["content"]]
        self.assertIn("document", types)
        self.assertIn("image", types)
        self.assertEqual(user_message["content"][0]["text"], "Put these on the calendar")
        self.assertEqual(AgentMessage.objects.filter(role=AgentMessage.Role.USER).count(), 1)

        # The reply and both attachments show in the person's Ask chat.
        self.client.force_login(self.ike)
        page = self.client.get(reverse("web:ask"))
        self.assertContains(page, "letter.pdf, Photo")
        self.assertContains(page, "Put these on the calendar")
        self.assertContains(page, "I found two dates.")

    def test_heic_photo_is_refused_with_a_clear_message(self):
        heic = SimpleUploadedFile("IMG_0001.HEIC", b"\x00\x00\x00\x18ftypheic", content_type="image/heic")
        response = self.share({"file": heic})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), HEIC_MESSAGE)
        self.assertFalse(AgentJob.objects.exists())

    def test_one_bad_file_queues_nothing_and_names_the_file(self):
        bad = SimpleUploadedFile("setup.exe", b"MZ", content_type="application/octet-stream")
        response = self.share({"message": "Read these", "file": [_pdf(), bad]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("setup.exe", response.content.decode())
        self.assertFalse(AgentJob.objects.exists())

    def test_too_many_files_are_refused(self):
        files = [_png(f"p{i}.png") for i in range(MAX_SHARE_FILES + 1)]
        response = self.share({"file": files})
        self.assertEqual(response.status_code, 400)
        self.assertIn(str(MAX_SHARE_FILES), response.content.decode())
        self.assertFalse(AgentJob.objects.exists())

    @override_settings(AGENT_PHOTOS_ENABLED=False)
    def test_upload_flags_still_apply(self):
        response = self.share({"file": _png()})
        self.assertEqual(response.status_code, 400)
        self.assertIn("switched off", response.content.decode())

    def test_web_send_still_takes_one_attachment(self):
        self.client.force_login(self.ike)
        response = self.client.post(
            reverse("web:ask_send"), {"message": "Read this", "attachment": _pdf()}, **FRAGMENT
        )
        self.assertEqual(response.status_code, 200)
        job = AgentJob.objects.get()
        self.assertEqual(job.reply_metadata["attachment_kind"], "document")
        self.assertNotIn("source", job.reply_metadata)


@override_settings(**WEB_ASK_SETTINGS)
class ShareAutoApplyTests(TestCase):
    """A share with the person's own instruction adds new items straight away,
    like an emailed instruction; everything else still waits for a tap."""

    def setUp(self):
        self.ike = User.objects.create_user(username="ike", password="pw123456!")
        self.account = WebAccount.objects.create(user=self.ike, role=HouseholdMember.Role.IKE)
        self.key = self.account.issue_share_key()
        self.phone = Client(enforce_csrf_checks=True)

    def share(self, data):
        response = self.phone.post(SHARE_URL, data, HTTP_AUTHORIZATION=f"Bearer {self.key}")
        self.assertEqual(response.status_code, 200, response.content)

    def run_turn(self, *tool_calls, final="Done."):
        fake = FakeClient(reply(*tool_calls, stop_reason="tool_use"), reply(text(final)))
        run_jobs(claim_next_jobs(), client=fake)
        return fake

    def test_instruction_with_a_document_creates_the_task_without_a_tap(self):
        self.share({"message": "Remind me to pay this", "file": _pdf("bill.pdf")})
        self.assertTrue(AgentJob.objects.get().reply_metadata["share_instruction"])
        fake = self.run_turn(tool_use("propose_task", {"title": "Pay the council tax bill"}), final="Added it.")

        proposal = AgentProposal.objects.get()
        self.assertEqual(proposal.status, AgentProposal.Status.CONFIRMED)
        self.assertTrue(Task.objects.filter(title="Pay the council tax bill").exists())
        context = fake.calls[0]["messages"][1]["content"]
        self.assertIn("share sheet with the speaker's own instruction", context)
        self.assertIn("added straight away", context)

        # Ask shows it as done, with nothing left to confirm.
        self.client.force_login(self.ike)
        page = self.client.get(reverse("web:ask")).content.decode()
        self.assertIn("Pay the council tax bill", page)
        self.assertNotIn(reverse("web:ask_proposal_confirm", args=[proposal.pk]), page)

    def test_text_only_instruction_also_applies(self):
        self.share({"message": "Add a task to book the MOT"})
        self.run_turn(tool_use("propose_task", {"title": "Book the MOT"}))
        self.assertEqual(AgentProposal.objects.get().status, AgentProposal.Status.CONFIRMED)

    def test_blank_instruction_leaves_suggestions_waiting(self):
        # The shared file or text alone is untrusted: it can't add things by itself.
        self.share({"message": "", "file": "Dentist Tuesday 3pm, please add to calendar"})
        self.assertFalse(AgentJob.objects.get().reply_metadata["share_instruction"])
        fake = self.run_turn(tool_use("propose_task", {"title": "Dentist"}))
        self.assertEqual(AgentProposal.objects.get().status, AgentProposal.Status.PENDING)
        self.assertFalse(Task.objects.filter(title="Dentist").exists())
        self.assertIn("without an instruction", fake.calls[0]["messages"][1]["content"])

    def test_changes_to_existing_items_still_wait(self):
        task = Task.objects.create(title="Renew passport")
        self.share({"message": "I've done this one, tick it off"})
        self.run_turn(tool_use("propose_complete_task", {"task_id": task.pk}))
        self.assertEqual(AgentProposal.objects.get().status, AgentProposal.Status.PENDING)
        task.refresh_from_db()
        self.assertNotEqual(task.status, Task.Status.COMPLETED)

    def test_typed_ask_messages_are_unchanged(self):
        self.client.force_login(self.ike)
        self.client.post(reverse("web:ask_send"), {"message": "Add a task to call the GP"}, **FRAGMENT)
        fake = self.run_turn(tool_use("propose_task", {"title": "Call the GP"}))
        self.assertEqual(AgentProposal.objects.get().status, AgentProposal.Status.PENDING)
        self.assertNotIn("share sheet", fake.calls[0]["messages"][1]["content"])

    def test_a_later_typed_turn_in_the_same_chat_goes_back_to_confirming(self):
        self.share({"message": "Add a task to book the MOT"})
        self.run_turn(tool_use("propose_task", {"title": "Book the MOT"}))
        self.client.force_login(self.ike)
        self.client.post(reverse("web:ask_send"), {"message": "And one to wash the car"}, **FRAGMENT)
        self.run_turn(tool_use("propose_task", {"title": "Wash the car"}, tool_id="toolu_2"))
        statuses = dict(AgentProposal.objects.values_list("payload__title", "status"))
        self.assertEqual(statuses, {"Book the MOT": "confirmed", "Wash the car": "pending"})


@override_settings(**WEB_ASK_SETTINGS)
class SettingsPageTests(TestCase):
    def setUp(self):
        self.ike = User.objects.create_user(username="ike", password="pw123456!", is_staff=True)
        self.wife = User.objects.create_user(username="wife", first_name="Ada", password="pw123456!")
        self.ike_account = WebAccount.objects.create(user=self.ike, role=HouseholdMember.Role.IKE)
        self.wife_account = WebAccount.objects.create(user=self.wife, role=HouseholdMember.Role.WIFE)

    def test_requires_login(self):
        response = self.client.get(reverse("web:settings"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_make_key_shows_it_once_then_only_the_hint(self):
        self.client.force_login(self.wife)
        page = self.client.get(reverse("web:settings"))
        self.assertContains(page, "Share from iPhone")
        self.assertContains(page, "No key yet")
        self.assertContains(page, "http://testserver/ask/share/")
        self.assertNotContains(page, reverse("admin:index"))

        response = self.client.post(reverse("web:share_key_create"))
        self.assertRedirects(response, reverse("web:settings") + "#share", fetch_redirect_response=False)
        self.wife_account.refresh_from_db()
        self.assertTrue(self.wife_account.has_share_key)

        shown = self.client.get(reverse("web:settings"))
        key = shown.context["new_key"]
        self.assertTrue(key.startswith("lifeos_"))
        self.assertContains(shown, key)
        self.assertEqual(shown["Cache-Control"], "no-store")
        self.assertEqual(WebAccount.for_share_key(key), self.wife_account)

        again = self.client.get(reverse("web:settings"))
        self.assertNotContains(again, key)
        self.assertContains(again, f"Key ending …{key[-4:]}")
        self.assertContains(again, "not used yet")
        self.assertTrue(AuditLog.objects.filter(action="share_key_created").exists())

    def test_turn_off_revokes_the_key(self):
        key = self.wife_account.issue_share_key()
        self.client.force_login(self.wife)
        self.client.post(reverse("web:share_key_revoke"))
        self.assertIsNone(WebAccount.for_share_key(key))
        page = self.client.get(reverse("web:settings"))
        self.assertContains(page, "No key yet")
        self.assertTrue(AuditLog.objects.filter(action="share_key_revoked").exists())

    def test_staff_see_admin_link_and_role(self):
        self.client.force_login(self.ike)
        page = self.client.get(reverse("web:settings"))
        self.assertContains(page, reverse("admin:index"))
        self.assertContains(page, "Ike’s account")

    def test_unlinked_login_gets_a_notice_and_cannot_make_a_key(self):
        guest = User.objects.create_user(username="guest", password="pw123456!")
        self.client.force_login(guest)
        page = self.client.get(reverse("web:settings"))
        self.assertContains(page, "isn’t linked to a household role")
        self.assertNotContains(page, "Make a key")
        response = self.client.post(reverse("web:share_key_create"), follow=True)
        self.assertContains(response, "isn&#x27;t available")

    @override_settings(AGENT_WEB_CHAT_ENABLED=False)
    def test_ask_off_hides_the_key_card(self):
        self.client.force_login(self.wife)
        page = self.client.get(reverse("web:settings"))
        self.assertContains(page, "Ask is switched off")
        self.assertNotContains(page, "Make a key")

    def test_post_only_for_key_actions(self):
        self.client.force_login(self.wife)
        self.assertEqual(self.client.get(reverse("web:share_key_create")).status_code, 405)
        self.assertEqual(self.client.get(reverse("web:share_key_revoke")).status_code, 405)


@override_settings(**WEB_ASK_SETTINGS)
class MenuDrawerTests(TestCase):
    def setUp(self):
        self.ike = User.objects.create_user(username="ike", password="pw123456!", is_staff=True)
        self.wife = User.objects.create_user(username="wife", first_name="Ada", password="pw123456!")
        WebAccount.objects.create(user=self.ike, role=HouseholdMember.Role.IKE)
        WebAccount.objects.create(user=self.wife, role=HouseholdMember.Role.WIFE)

    def test_hamburger_opens_a_drawer_with_name_settings_and_admin_for_staff(self):
        self.client.force_login(self.ike)
        for name in ["web:upcoming", "web:task_list", "web:note_list", "web:calendar", "web:settings"]:
            page = self.client.get(reverse(name))
            self.assertContains(page, 'data-open-dialog="menuDrawer"', msg_prefix=name)
            self.assertContains(page, 'id="menuDrawer"', msg_prefix=name)
        page = self.client.get(reverse("web:upcoming"))
        self.assertNotContains(page, "accountSheet")
        drawer = page.content.decode().split('id="menuDrawer"', 1)[1].split("</dialog>", 1)[0]
        self.assertIn("Ike", drawer)
        self.assertIn("Signed in as ike", drawer)
        self.assertIn(reverse("web:settings"), drawer)
        self.assertIn(reverse("admin:index"), drawer)
        for name in ["web:upcoming", "web:calendar", "web:ask", "web:task_list", "web:note_list", "web:review_list"]:
            self.assertIn(f'href="{reverse(name)}"', drawer, name)
        # The tile for the page you're on is marked as current.
        tasks_page = self.client.get(reverse("web:task_list")).content.decode()
        tasks_drawer = tasks_page.split('id="menuDrawer"', 1)[1].split("</dialog>", 1)[0]
        self.assertRegex(tasks_drawer, r'href="/tasks/"\s+aria-current="page"')

    def test_drawer_review_tile_shows_the_waiting_count(self):
        ParsedAction.objects.create(
            incoming_email=IncomingEmail.objects.create(
                gmail_message_id="msg-drawer-1", outer_sender="ike@example.com", subject="Please add a task",
            ),
            action_type=ParsedAction.ActionType.CREATE_TASK,
            title="Renew passport",
            status=ParsedAction.Status.PENDING_REVIEW,
            confidence=0.5,
        )
        self.client.force_login(self.ike)
        page = self.client.get(reverse("web:upcoming")).content.decode()
        drawer = page.split('id="menuDrawer"', 1)[1].split("</dialog>", 1)[0]
        self.assertIn('class="app-badge"', drawer)
        self.assertIn("1 waiting", drawer)

    @override_settings(AGENT_WEB_CHAT_ENABLED=False)
    def test_drawer_leaves_out_ask_when_it_is_off(self):
        self.client.force_login(self.ike)
        page = self.client.get(reverse("web:upcoming")).content.decode()
        drawer = page.split('id="menuDrawer"', 1)[1].split("</dialog>", 1)[0]
        self.assertNotIn("app-ask", drawer)
        self.assertIn("app-tasks", drawer)

    def test_drawer_hides_admin_from_non_staff_and_uses_their_name(self):
        self.client.force_login(self.wife)
        page = self.client.get(reverse("web:upcoming"))
        drawer = page.content.decode().split('id="menuDrawer"', 1)[1].split("</dialog>", 1)[0]
        self.assertIn("Ada", drawer)
        self.assertNotIn(reverse("admin:index"), drawer)
        self.assertIn(reverse("web:settings"), drawer)
