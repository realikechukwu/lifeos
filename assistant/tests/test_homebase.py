import base64
import http.server
import json
import threading
from datetime import timedelta
from io import BytesIO, StringIO
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from assistant.agent.loop import READ_ONLY_TOOLS, _run_tool
from assistant.agent.tools import ACTIVE_TOOL_DEFINITIONS, HANDLERS, TOOL_DEFINITIONS, ToolContext, ToolInputError
from assistant.agent.briefings import DAILY, WEEKLY, briefing_prompt
from assistant.agent.homebase import home_admin, home_admin_note
from assistant.agent.proposals import decide, summary
from assistant.agent.weekly_email import write_shared
from assistant.services import homebase
from assistant.services.telegram_handlers import process_telegram_update
from assistant.tests.test_agent import AGENT_SETTINGS, AgentFakeBot, FakeClient, reply, text
from assistant.tests.test_telegram import message_update
from core.models import AgentJob, AgentMessage, AgentProposal, AgentThread

PASSWORD = "correct-horse-battery"
HOMEBASE_SETTINGS = {
    "AGENT_HOMEBASE_ENABLED": True,
    "HOMEBASE_SUPABASE_URL": "https://homebase.example.supabase.co/",
    "HOMEBASE_SUPABASE_ANON_KEY": "anon-key",
    "HOMEBASE_AGENT_EMAIL": "lifeos@example.com",
    "HOMEBASE_AGENT_PASSWORD": PASSWORD,
}
DOC_ID = "0b6d3c1e-7f2a-4c55-9e1d-2a3b4c5d6e7f"
TOOL_NAMES = {"homebase_snags", "homebase_documents", "homebase_insurance", "homebase_wifi", "homebase_read_document"}
WRITE_TOOL_NAMES = {"propose_homebase_snag", "propose_homebase_document", "propose_homebase_memory_aid", "propose_homebase_insurance"}
SNAG_ID = "5a1e2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b"
POLICY_ID = "7c8d9e0f-1a2b-4c3d-8e4f-5a6b7c8d9e0f"
BASE_UPDATED = "2026-10-01T09:00:00.123456+00:00"


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSupabase:
    """Stands in for urlopen: signs in, serves tables and stored files."""

    def __init__(self, tables=None, files=None):
        self.tables = tables or {}
        self.files = files or {}
        self.requests = []
        self.writes = []
        self.sign_ins = 0
        self.reject_next_token = False
        self.update_matches = True
        self.reject_insert = False

    def __call__(self, request, timeout=None):
        url = urlsplit(request.full_url)
        self.requests.append(request)
        method = request.get_method()
        if url.path == "/auth/v1/token":
            self.sign_ins += 1
            return FakeResponse(json.dumps({"access_token": f"token-{self.sign_ins}", "expires_in": 3600,
                                            "user": {"id": "lifeos-user"}}).encode())
        if self.reject_next_token:
            self.reject_next_token = False
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        if url.path.startswith("/rest/v1/"):
            table = url.path.removeprefix("/rest/v1/")
            if method == "GET":
                return FakeResponse(json.dumps(self.tables[table]).encode())
            body = json.loads(request.data)
            self.writes.append((method, table, parse_qs(url.query), body))
            if method == "POST" and self.reject_insert:
                error = BytesIO(b'{"code": "42501", "message": "new row violates row-level security policy"}')
                raise HTTPError(request.full_url, 403, "Forbidden", {}, error)
            if method == "PATCH" and not self.update_matches:
                return FakeResponse(b"[]")
            return FakeResponse(json.dumps([{"id": "new-snag", **body}]).encode())
        if method in {"POST", "DELETE"} and url.path.startswith("/storage/v1/object/"):
            bucket, path = url.path.removeprefix("/storage/v1/object/").split("/", 1)
            self.writes.append((method, bucket, path, request.data))
            return FakeResponse(b"{}")
        return FakeResponse(self.files[url.path.removeprefix("/storage/v1/object/authenticated/documents/")])

    def queries(self, table):
        return [parse_qs(urlsplit(r.full_url).query) for r in self.requests if urlsplit(r.full_url).path == f"/rest/v1/{table}"]


def _ctx():
    return ToolContext(role="ike", speaker_name="Ike")


def _call(name, args):
    return json.loads(HANDLERS[name](_ctx(), args))


class ChunkedSupabase(http.server.BaseHTTPRequestHandler):
    """Answers like the real Supabase: chunked bodies with no Content-Length."""
    protocol_version = "HTTP/1.1"

    def _send(self, body):
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        half = len(body) // 2
        for chunk in (body[:half], body[half:]):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        self.wfile.write(b"0\r\n\r\n")

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self._send(b'{"access_token": "token-1", "expires_in": 3600, "user": {"id": "lifeos-user"}}')

    def do_GET(self):
        self._send(b'[{"id": "a"}]')

    def log_message(self, *args):
        pass


@override_settings(**HOMEBASE_SETTINGS)
class HomebaseClientTests(SimpleTestCase):
    def setUp(self):
        homebase._token.update(value="", expires_at=0.0)

    def test_signs_in_once_and_reuses_the_token(self):
        fake = FakeSupabase(tables={"snags": [], "wifi_settings": []})
        with patch("assistant.services.homebase.urlopen", fake):
            homebase.select("snags", columns="id")
            homebase.select("wifi_settings", columns="network_name")
        self.assertEqual(fake.sign_ins, 1)
        sign_in, read = fake.requests[0], fake.requests[1]
        self.assertEqual(json.loads(sign_in.data), {"email": "lifeos@example.com", "password": PASSWORD})
        self.assertEqual(read.full_url.split("?")[0], "https://homebase.example.supabase.co/rest/v1/snags")
        self.assertEqual(read.get_header("Authorization"), "Bearer token-1")
        self.assertEqual(read.get_header("Apikey"), "anon-key")

    def test_a_revoked_token_signs_in_again_once(self):
        fake = FakeSupabase(tables={"snags": [{"id": "a"}]})
        with patch("assistant.services.homebase.urlopen", fake):
            homebase.select("snags", columns="id")
            fake.reject_next_token = True
            self.assertEqual(homebase.select("snags", columns="id"), [{"id": "a"}])
        self.assertEqual(fake.sign_ins, 2)
        self.assertEqual(fake.requests[-1].get_header("Authorization"), "Bearer token-2")

    def test_errors_never_carry_the_password(self):
        def refuse(request, timeout=None):
            raise HTTPError(request.full_url, 400, f"bad password {PASSWORD}", {}, None)

        with patch("assistant.services.homebase.urlopen", refuse):
            with self.assertRaises(homebase.HomebaseError) as caught:
                homebase.select("snags", columns="id")
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    @override_settings(HOMEBASE_AGENT_PASSWORD="")
    def test_unconfigured_client_refuses(self):
        self.assertFalse(homebase.configured())
        with self.assertRaisesMessage(homebase.HomebaseError, "not configured"):
            homebase.select("snags", columns="id")

    def test_storage_path_matches_homebase(self):
        base = "https://homebase.example.supabase.co/storage/v1/object"
        self.assertEqual(homebase.storage_path("1700-abc.pdf", "documents"), "1700-abc.pdf")
        self.assertEqual(homebase.storage_path(f"{base}/public/documents/a%20b.pdf", "documents"), "a b.pdf")
        self.assertEqual(homebase.storage_path(f"{base}/sign/documents/x/y.pdf?token=t", "documents"), "x/y.pdf")
        self.assertIsNone(homebase.storage_path(f"{base}/public/snag-photos/y.jpg", "documents"))
        self.assertIsNone(homebase.storage_path("", "documents"))

    def test_reads_chunked_responses_from_a_real_server(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), ChunkedSupabase)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with self.settings(HOMEBASE_SUPABASE_URL=f"http://127.0.0.1:{server.server_port}"):
            self.assertEqual(homebase.select("snags", columns="id"), [{"id": "a"}])
            self.assertEqual(homebase.download("documents", "a.pdf", max_bytes=100), b'[{"id": "a"}]')

    def test_download_stops_at_the_size_limit(self):
        fake = FakeSupabase(files={"big.pdf": b"x" * 11})
        with patch("assistant.services.homebase.urlopen", fake):
            with self.assertRaises(homebase.HomebaseFileTooLargeError):
                homebase.download("documents", "big.pdf", max_bytes=10)


@override_settings(**HOMEBASE_SETTINGS)
class HomebaseToolTests(SimpleTestCase):
    def setUp(self):
        homebase._token.update(value="", expires_at=0.0)

    def _fake(self, **kwargs):
        fake = FakeSupabase(**kwargs)
        patcher = patch("assistant.services.homebase.urlopen", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_tools_are_registered_and_off_by_default(self):
        names = TOOL_NAMES | WRITE_TOOL_NAMES
        self.assertTrue(names <= {t["name"] for t in TOOL_DEFINITIONS})
        self.assertTrue(names <= set(HANDLERS))
        self.assertTrue(TOOL_NAMES <= READ_ONLY_TOOLS)
        self.assertFalse(WRITE_TOOL_NAMES & READ_ONLY_TOOLS)
        # The test environment starts with AGENT_HOMEBASE_ENABLED unset.
        self.assertFalse(names & {t["name"] for t in ACTIVE_TOOL_DEFINITIONS})

    @override_settings(AGENT_HOMEBASE_ENABLED=False)
    def test_disabled_tools_refuse(self):
        with self.assertRaisesMessage(ToolInputError, "switched off"):
            HANDLERS["homebase_insurance"](_ctx(), {})

    def test_unresolved_snags_filtered_by_room(self):
        fake = self._fake(tables={"snags": [
            {"id": "1", "title": "Dripping tap", "description": "x" * 600, "room": "Main Bathroom", "status": "open",
             "severity": "low", "photo_url": "p.jpg", "created_at": "2026-10-01T09:00:00+00:00", "resolved_at": None},
            {"id": "2", "title": "Loose handle", "description": None, "room": "Kitchen", "status": "in_progress",
             "severity": "medium", "photo_url": None, "created_at": "2026-10-02T09:00:00+00:00", "resolved_at": None},
        ]})
        result = _call("homebase_snags", {"status": "unresolved", "room": "bathroom"})
        self.assertEqual(fake.queries("snags")[0]["status"], ["neq.resolved"])
        self.assertEqual([s["title"] for s in result["snags"]], ["Dripping tap"])
        snag = result["snags"][0]
        self.assertTrue(snag["has_photo"])
        self.assertEqual(snag["reported_on"], "2026-10-01")
        self.assertEqual(len(snag["description"]), 501)

    def test_documents_expiring_soon(self):
        today = timezone.localdate()
        fake = self._fake(tables={"documents": [
            {"id": "a", "title": "Gas safety certificate", "category": "property", "subcategory": None, "file_name": "gas.pdf",
             "file_url": "gas.pdf", "notes": "", "expiry_date": str(today + timedelta(days=20)), "reminder_date": None},
            {"id": "b", "title": "EPC", "category": "property", "subcategory": None, "file_name": None,
             "file_url": None, "notes": "", "expiry_date": str(today + timedelta(days=400)), "reminder_date": None},
            {"id": "c", "title": "Electrical report", "category": "property", "subcategory": None, "file_name": "eicr.pdf",
             "file_url": "eicr.pdf", "notes": "", "expiry_date": str(today - timedelta(days=3)), "reminder_date": None},
            {"id": "d", "title": "Floor plan", "category": "property", "subcategory": None, "file_name": None,
             "file_url": None, "notes": "", "expiry_date": None, "reminder_date": None},
        ]})
        result = _call("homebase_documents", {"category": "property", "search": "", "expiring_within_days": 30})
        self.assertEqual(fake.queries("documents")[0]["category"], ["eq.property"])
        self.assertEqual([d["id"] for d in result["documents"]], ["c", "a"])

    def test_memory_aids_say_where_things_are(self):
        self._fake(tables={"documents": [
            {"id": "a", "title": "Water stopcock", "category": "property", "subcategory": "Memory Aid", "file_name": "s.jpg",
             "file_url": "s.jpg", "notes": 'MEMORY_AID::{"location_description":"Under the sink","notes":"Turn clockwise"}',
             "expiry_date": None, "reminder_date": None},
        ]})
        doc = _call("homebase_documents", {"category": "any", "search": "sink", "expiring_within_days": 0})["documents"][0]
        self.assertTrue(doc["memory_aid"])
        self.assertEqual(doc["kept_where"], "Under the sink")
        self.assertEqual(doc["notes"], "Turn clockwise")

    def test_documents_search_and_file_names(self):
        self._fake(tables={"documents": [
            {"id": "a", "title": "Boiler warranty", "category": "property", "subcategory": None, "file_name": "w.pdf",
             "file_url": None, "notes": "Worcester Bosch", "expiry_date": None, "reminder_date": None},
            {"id": "b", "title": "Water bill", "category": "utility", "subcategory": None, "file_name": None,
             "file_url": None, "notes": "", "expiry_date": None, "reminder_date": None},
        ]})
        result = _call("homebase_documents", {"category": "any", "search": "bosch", "expiring_within_days": 0})
        self.assertEqual([d["id"] for d in result["documents"]], ["a"])
        self.assertIsNone(result["documents"][0]["file_name"])

    def test_wifi_never_reads_the_router_admin_table(self):
        fake = self._fake(tables={"wifi_settings": [{"network_name": "Home", "password": "pw", "encryption": "WPA",
                                                     "is_guest": False, "welcome_message": None}]})
        HANDLERS["homebase_wifi"](_ctx(), {})
        tables = {urlsplit(r.full_url).path for r in fake.requests}
        self.assertNotIn("/rest/v1/wifi_admin_credentials", tables)
        self.assertIn("/rest/v1/wifi_settings", tables)

    def test_read_document_extracts_pdf_text(self):
        fake = self._fake(
            tables={"documents": [{"id": DOC_ID, "title": "Home insurance", "category": "insurance",
                                   "file_name": "Policy.PDF", "file_url": "1700-policy.pdf", "notes": ""}]},
            files={"1700-policy.pdf": b"%PDF-fake"},
        )
        with patch("assistant.agent.homebase.extract_text", return_value="Excess £250") as extract:
            result = _call("homebase_read_document", {"document_id": DOC_ID.upper()})
        extract.assert_called_once_with(b"%PDF-fake", "application/pdf")
        self.assertEqual(fake.queries("documents")[0]["id"], [f"eq.{DOC_ID}"])
        self.assertEqual(result["text"], "Excess £250")
        self.assertFalse(result["truncated"])
        self.assertIn("Untrusted", result["note"])

    def test_read_document_returns_images_as_image_blocks(self):
        self._fake(
            tables={"documents": [{"id": DOC_ID, "title": "Meter reading", "category": "utility",
                                   "file_name": "meter.jpg", "file_url": "meter.jpg", "notes": ""}]},
            files={"meter.jpg": b"jpeg-bytes"},
        )
        blocks = HANDLERS["homebase_read_document"](_ctx(), {"document_id": DOC_ID})
        self.assertEqual(blocks[1]["source"], {"type": "base64", "media_type": "image/jpeg",
                                               "data": base64.b64encode(b"jpeg-bytes").decode()})
        self.assertEqual(json.loads(blocks[0]["text"])["title"], "Meter reading")
        result = _run_tool(_ctx(), SimpleNamespace(name="homebase_read_document", id="toolu_1", input={"document_id": DOC_ID}))
        self.assertEqual(result["content"][1]["type"], "image")

    def test_read_document_refusals(self):
        self._fake(tables={"documents": [{"id": DOC_ID, "title": "Floor plan", "category": "property",
                                          "file_name": None, "file_url": None, "notes": ""}]})
        with self.assertRaisesMessage(ToolInputError, "id returned by homebase_documents"):
            HANDLERS["homebase_read_document"](_ctx(), {"document_id": "1 or 1=1"})
        with self.assertRaisesMessage(ToolInputError, "no stored file"):
            HANDLERS["homebase_read_document"](_ctx(), {"document_id": DOC_ID})

    def test_unreachable_homebase_is_explained(self):
        def down(request, timeout=None):
            raise TimeoutError

        with patch("assistant.services.homebase.urlopen", down):
            with self.assertRaisesMessage(ToolInputError, "Homebase could not be read"):
                HANDLERS["homebase_insurance"](_ctx(), {})


@override_settings(**HOMEBASE_SETTINGS)
class HomebaseCheckCommandTests(SimpleTestCase):
    def setUp(self):
        homebase._token.update(value="", expires_at=0.0)

    def test_prints_counts_without_secrets(self):
        tables = {"snags": [{}, {}], "documents": [{}], "insurance_summary": [], "wifi_settings": [{}]}
        out = StringIO()
        with patch("assistant.services.homebase.urlopen", FakeSupabase(tables=tables)):
            call_command("homebase_check", stdout=out)
        self.assertIn("snags: 2", out.getvalue())
        self.assertNotIn(PASSWORD, out.getvalue())

    def test_an_account_that_sees_nothing_fails(self):
        tables = {"snags": [], "documents": [], "insurance_summary": [], "wifi_settings": []}
        with patch("assistant.services.homebase.urlopen", FakeSupabase(tables=tables)):
            with self.assertRaisesMessage(CommandError, "approved Homebase owner"):
                call_command("homebase_check", stdout=StringIO())


@override_settings(**AGENT_SETTINGS, **HOMEBASE_SETTINGS, AGENT_PROPOSALS_ENABLED=True)
class HomebaseProposalTests(TestCase):
    def setUp(self):
        homebase._token.update(value="", expires_at=0.0, user_id="")
        bot = AgentFakeBot()
        process_telegram_update(message_update(1, text="Log a snag"), bot=bot)
        job = AgentJob.objects.get()
        self.thread = AgentThread.objects.create(
            channel="telegram", role="ike", telegram_user=job.telegram_user,
            telegram_chat=job.telegram_chat, last_activity_at=timezone.now(),
        )
        self.ctx = ToolContext(role="ike", speaker_name="Ike", thread=self.thread,
                               telegram_user=job.telegram_user, telegram_chat=job.telegram_chat, bot=bot)
        self.fake = FakeSupabase(tables={
            "snags": [{"id": SNAG_ID, "updated_at": BASE_UPDATED, "title": "Dripping tap", "room": "Kitchen",
                       "description": None, "status": "open", "severity": "low"}],
            "insurance_summary": [{"id": POLICY_ID, "updated_at": BASE_UPDATED, "insurer_name": "Aviva",
                                   "policy_type": "home", "policy_number": "HX1", "claims_phone": "0800 000",
                                   "renewal_date": "2026-12-01", "buildings_excess": 200, "contents_excess": None,
                                   "key_exclusions": None, "notes": None}],
            "documents": [{"id": DOC_ID, "updated_at": BASE_UPDATED, "title": "Boiler reset",
                           "expiry_date": None, "reminder_date": None,
                           "notes": 'MEMORY_AID::{"location_description":"Airing cupboard"}'}],
        })
        patcher = patch("assistant.services.homebase.urlopen", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def propose(self, tool, args):
        result = json.loads(HANDLERS[tool](self.ctx, args))
        return AgentProposal.objects.get(pk=result["proposal_id"])

    def send_photo(self, data=b"jpeg-bytes"):
        AgentMessage.objects.create(thread=self.thread, role="user", content=[], content_json=json.dumps([
            {"type": "text", "text": "This is leaking"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(data).decode()}},
        ]))

    def send_pdf(self, name="Gas Safety (2026).pdf", data=b"%PDF-1.7"):
        AgentMessage.objects.create(thread=self.thread, role="user", content=[], content_json=json.dumps([
            {"type": "text", "text": "File this please"},
            {"type": "text", "text": f"Untrusted uploaded document: {name}"},
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.b64encode(data).decode()}},
        ]))

    def test_new_snag_with_the_chat_photo_waits_for_confirm(self):
        self.send_photo()
        proposal = self.propose("propose_homebase_snag", {
            "title": "Leaking radiator valve", "room": "Hall", "severity": "high", "attach_photo": True,
        })
        self.assertEqual(proposal.status, "pending")
        self.assertFalse(self.fake.writes)
        card = summary(proposal)
        self.assertIn("Log Homebase snag", card)
        self.assertIn("Photo: attached from this chat", card)

        decide(proposal, confirm=True)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "confirmed")
        self.assertEqual(proposal.result["snag_id"], "new-snag")
        (_, bucket, path, data), (method, table, _, row) = self.fake.writes
        self.assertEqual((bucket, data), ("snag-photos", b"jpeg-bytes"))
        self.assertTrue(path.endswith(".jpg"))
        self.assertEqual((method, table), ("POST", "snags"))
        self.assertEqual(row, {"title": "Leaking radiator valve", "room": "Hall", "severity": "high", "status": "open",
                               "description": None, "photo_url": path, "reported_by": "lifeos-user"})

    def test_photo_must_come_from_the_conversation(self):
        with self.assertRaisesMessage(ToolInputError, "no photo in this conversation"):
            self.propose("propose_homebase_snag", {"title": "Crack", "room": "Hall", "attach_photo": True})
        proposal = self.propose("propose_homebase_snag", {"title": "Crack", "room": "Hall",
                                                          "photo": {"message_id": 1, "index": 0}})
        self.assertNotIn("photo", proposal.payload)
        self.assertEqual(proposal.payload["severity"], "medium")

    def test_failed_insert_removes_the_uploaded_photo(self):
        self.send_photo()
        proposal = self.propose("propose_homebase_snag", {"title": "Crack", "room": "Hall", "attach_photo": True})
        self.fake.reject_insert = True
        decide(proposal, confirm=True)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "failed")
        self.assertIn("row-level security", proposal.result["detail"])
        self.assertEqual([w[0] for w in self.fake.writes], ["POST", "POST", "DELETE"])

    def test_resolving_a_snag_only_saves_if_unchanged_since_proposed(self):
        proposal = self.propose("propose_homebase_snag", {"snag_id": SNAG_ID, "status": "resolved", "severity": "low"})
        self.assertEqual(proposal.payload["changes"], {"status": "resolved"})
        self.assertIn("Status: open → resolved", summary(proposal))
        self.assertIn("Dripping tap (Kitchen)", summary(proposal))

        decide(proposal, confirm=True)
        method, table, query, values = self.fake.writes[0]
        self.assertEqual((method, table), ("PATCH", "snags"))
        self.assertEqual(query, {"id": [f"eq.{SNAG_ID}"], "updated_at": [f"eq.{BASE_UPDATED}"]})
        self.assertEqual(values["status"], "resolved")
        self.assertEqual(values["resolved_at"], values["updated_at"])

    def test_a_record_edited_in_homebase_meanwhile_is_not_overwritten(self):
        proposal = self.propose("propose_homebase_snag", {"snag_id": SNAG_ID, "status": "in_progress"})
        self.fake.update_matches = False
        decide(proposal, confirm=True)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "failed")
        self.assertIn("changed in Homebase", proposal.result["detail"])

    def test_insurance_amounts_and_card(self):
        proposal = self.propose("propose_homebase_insurance", {
            "policy_id": POLICY_ID, "buildings_excess": "250", "renewal_date": "2027-12-01",
        })
        self.assertEqual(proposal.payload["changes"], {"buildings_excess": 250.0, "renewal_date": "2027-12-01"})
        card = summary(proposal)
        self.assertIn("Aviva home policy", card)
        self.assertIn("Buildings excess: £200.00 → £250.00", card)
        self.assertIn("Renewal date: 2026-12-01 → 2027-12-01", card)

    def test_invalid_changes_are_refused(self):
        for tool, args, message in [
            ("propose_homebase_snag", {"snag_id": SNAG_ID, "severity": "low"}, "unchanged"),
            ("propose_homebase_snag", {"snag_id": SNAG_ID, "title": None}, "cannot be cleared"),
            ("propose_homebase_snag", {"snag_id": "1", "status": "resolved"}, "id returned"),
            ("propose_homebase_insurance", {"policy_id": POLICY_ID, "renewal_date": "1 Dec"}, "YYYY-MM-DD"),
            ("propose_homebase_insurance", {"policy_id": POLICY_ID, "contents_excess": True}, "amount"),
            ("propose_homebase_document", {"document_id": DOC_ID, "notes": "Replace"}, "structured data"),
            ("propose_homebase_snag", {"title": "Crack"}, "title and a room"),
        ]:
            with self.subTest(args=args), self.assertRaisesMessage(ToolInputError, message):
                self.propose(tool, args)
        self.assertEqual(AgentProposal.objects.count(), 0)

    def test_one_tool_per_record_creates_or_changes(self):
        self.send_photo()
        with self.assertRaisesMessage(ToolInputError, "only be attached to a new snag"):
            self.propose("propose_homebase_snag", {"snag_id": SNAG_ID, "status": "resolved", "attach_photo": True})
        with self.assertRaisesMessage(ToolInputError, "Unexpected fields"):
            self.propose("propose_homebase_document", {"document_id": DOC_ID, "category": "utility"})
        created = self.propose("propose_homebase_snag", {"title": "Crack", "room": "Hall"})
        changed = self.propose("propose_homebase_snag", {"snag_id": SNAG_ID, "status": "resolved"})
        self.assertEqual((created.kind, changed.kind), ("homebase_create_snag", "homebase_update_snag"))

    def test_document_dates_can_be_set_and_cleared(self):
        proposal = self.propose("propose_homebase_document", {"document_id": DOC_ID, "expiry_date": "2027-03-01"})
        self.assertIn("Expiry date: (none) → 2027-03-01", summary(proposal))

    def test_switching_homebase_off_stops_pending_changes(self):
        proposal = self.propose("propose_homebase_snag", {"snag_id": SNAG_ID, "status": "resolved"})
        with self.settings(AGENT_HOMEBASE_ENABLED=False):
            decide(proposal, confirm=True)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "failed")
        self.assertFalse(self.fake.writes)

    def test_memory_aid_from_the_chat_photo(self):
        self.send_photo(b"stopcock-jpeg")
        proposal = self.propose("propose_homebase_memory_aid", {
            "title": "Water stopcock", "location_description": "Under the kitchen sink, behind the bin",
        })
        card = summary(proposal)
        self.assertIn("Save Homebase memory aid", card)
        self.assertIn("Kept: Under the kitchen sink, behind the bin", card)
        self.assertIn("File: Water_stopcock.jpg, from this chat", card)

        decide(proposal, confirm=True)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "confirmed", proposal.result)
        (_, bucket, path, data), (_, table, _, row) = self.fake.writes
        self.assertEqual((bucket, data, table), ("documents", b"stopcock-jpeg", "documents"))
        self.assertTrue(path.endswith("-Water_stopcock.jpg"))
        self.assertEqual(row, {
            "title": "Water stopcock", "file_url": path, "file_name": "Water_stopcock.jpg", "file_size": 13,
            "uploaded_by": "lifeos-user", "category": "property", "subcategory": "Memory Aid",
            "expiry_date": None, "reminder_date": None,
            "notes": 'MEMORY_AID::{"location_description":"Under the kitchen sink, behind the bin"}',
        })

    def test_memory_aids_need_a_photo_not_a_pdf(self):
        self.send_pdf()
        with self.assertRaisesMessage(ToolInputError, "no photo in this conversation"):
            self.propose("propose_homebase_memory_aid", {"title": "Fuse box", "location_description": "Garage"})

    def test_document_from_a_pdf_keeps_its_name(self):
        self.send_pdf()
        proposal = self.propose("propose_homebase_document", {
            "title": "Gas safety certificate", "category": "property", "expiry_date": "2027-10-30",
            "file": {"message_id": 999, "index": 0},
        })
        self.assertEqual(proposal.payload["file"]["file_name"], "Gas_Safety__2026_.pdf")
        card = summary(proposal)
        self.assertIn("File Homebase document", card)
        self.assertIn("Expiry date: 2027-10-30", card)

        decide(proposal, confirm=True)
        (_, bucket, path, data), (_, table, _, row) = self.fake.writes
        self.assertEqual((bucket, data), ("documents", b"%PDF-1.7"))
        self.assertEqual(row["category"], "property")
        self.assertEqual(row["expiry_date"], "2027-10-30")
        self.assertEqual(row["file_name"], "Gas_Safety__2026_.pdf")
        self.assertIsNone(row["subcategory"])

    def test_document_rules(self):
        with self.assertRaisesMessage(ToolInputError, "no photo or PDF in this conversation"):
            self.propose("propose_homebase_document", {"title": "Warranty", "category": "property"})
        self.send_photo()
        for args, message in [
            ({"title": "Warranty"}, "Missing: category"),
            ({"title": "Boiler", "category": "property", "subcategory": "Memory aid"}, "propose_homebase_memory_aid"),
            ({"title": "Bill", "category": "council"}, "category must be one of"),
        ]:
            with self.subTest(args=args), self.assertRaisesMessage(ToolInputError, message):
                self.propose("propose_homebase_document", args)

    def test_new_insurance_policy(self):
        with self.assertRaisesMessage(ToolInputError, "Missing: claims_phone"):
            self.propose("propose_homebase_insurance", {"insurer_name": "Admiral", "policy_number": "AB1", "renewal_date": "2027-03-03"})
        proposal = self.propose("propose_homebase_insurance", {
            "insurer_name": "Admiral", "policy_type": "car", "policy_number": "AB1",
            "claims_phone": "0333 220 2000", "renewal_date": "2027-03-03", "contents_excess": 100,
        })
        card = summary(proposal)
        self.assertIn("Admiral car policy", card)
        self.assertIn("Policy number: AB1", card)
        self.assertIn("Contents excess: £100.00", card)

        decide(proposal, confirm=True)
        method, table, _, row = self.fake.writes[0]
        self.assertEqual((method, table), ("POST", "insurance_summary"))
        self.assertEqual(row["insurer_name"], "Admiral")
        self.assertEqual(row["contents_excess"], 100.0)
        self.assertIsNone(row["buildings_excess"])
        self.assertNotIn("title", row)


@override_settings(**AGENT_SETTINGS, **HOMEBASE_SETTINGS)
class HomebaseBriefingTests(TestCase):
    def setUp(self):
        homebase._token.update(value="", expires_at=0.0, user_id="")
        today = timezone.localdate()
        day = lambda offset: str(today + timedelta(days=offset))
        self.fake = FakeSupabase(tables={
            "documents": [
                {"title": "Gas safety certificate", "category": "property", "subcategory": None, "expiry_date": day(5)},
                {"title": "EICR", "category": "property", "subcategory": None, "expiry_date": day(-10)},
                {"title": "Old warranty", "category": "property", "subcategory": None, "expiry_date": day(-40)},
                {"title": "TV licence", "category": "utility", "subcategory": None, "expiry_date": day(20)},
                {"title": "Stopcock", "category": "property", "subcategory": "Memory Aid", "expiry_date": None},
            ],
            "insurance_summary": [
                {"insurer_name": "Aviva", "policy_type": "home", "renewal_date": day(25)},
                {"insurer_name": "Admiral", "policy_type": "car", "renewal_date": day(200)},
            ],
            "snags": [
                {"title": "Boiler leaking", "room": "Utility", "status": "open", "severity": "critical"},
                {"title": "Squeaky door", "room": "Hall", "status": "open", "severity": "low"},
            ],
        })
        patcher = patch("assistant.services.homebase.urlopen", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_week_ahead_window(self):
        found = home_admin(days=30, include_snags=True)
        self.assertEqual([d["title"] for d in found["documents_expiring"]], ["Gas safety certificate", "EICR", "TV licence"])
        self.assertEqual([p["insurer"] for p in found["insurance_renewing"]], ["Aviva"])
        self.assertEqual([s["title"] for s in found["urgent_snags"]], ["Boiler leaking"])
        self.assertEqual(self.fake.queries("snags")[0]["status"], ["neq.resolved"])

    def test_morning_window_skips_snags(self):
        found = home_admin(days=7, include_snags=False)
        self.assertEqual([d["title"] for d in found["documents_expiring"]], ["Gas safety certificate", "EICR"])
        self.assertNotIn("insurance_renewing", found)
        self.assertNotIn("urgent_snags", found)
        self.assertFalse(self.fake.queries("snags"))

    def test_off_or_unreachable_leaves_homebase_out(self):
        with self.settings(AGENT_HOMEBASE_ENABLED=False):
            self.assertIsNone(home_admin(days=30, include_snags=True))
            self.assertEqual(home_admin_note(days=30, include_snags=True), "")

        def down(request, timeout=None):
            raise TimeoutError

        with patch("assistant.services.homebase.urlopen", down):
            self.assertIsNone(home_admin(days=30, include_snags=True))

    def test_nothing_due_is_said_plainly(self):
        self.fake.tables = {"documents": [], "insurance_summary": [], "snags": []}
        self.assertIn("nothing due", home_admin_note(days=7, include_snags=False))

    def test_briefing_prompts_carry_homebase_only_when_on(self):
        today = timezone.localdate()
        daily, weekly = briefing_prompt(DAILY, today), briefing_prompt(WEEKLY, today)
        self.assertIn("Gas safety certificate", daily)
        self.assertNotIn("TV licence", daily)
        self.assertIn("**Home**:", daily)
        self.assertIn("TV licence", weekly)
        self.assertIn("Boiler leaking", weekly)
        self.assertIn("**Home**:", weekly)
        with self.settings(AGENT_HOMEBASE_ENABLED=False):
            for prompt in (briefing_prompt(DAILY, today), briefing_prompt(WEEKLY, today)):
                self.assertNotIn("Homebase", prompt)
                self.assertNotIn("**Home**", prompt)
                self.assertNotIn("\n\n\n", prompt)
                self.assertNotIn("{", prompt)

    @patch("assistant.agent.tools.list_calendar_events", return_value=[])
    def test_weekly_email_shared_section_includes_home_admin(self, _calendar):
        client = FakeClient(reply(text("Home: renew the gas safety certificate")))
        write_shared(timezone.localdate(), client=client)
        prompt = client.calls[0]["messages"][0]["content"][0]["text"]
        self.assertIn("Aviva", prompt)
        self.assertIn("Home: each Homebase document", prompt)

        with self.settings(AGENT_HOMEBASE_ENABLED=False):
            client = FakeClient(reply(text("Quiet week")))
            write_shared(timezone.localdate(), client=client)
        prompt = client.calls[0]["messages"][0]["content"][0]["text"]
        self.assertNotIn("Homebase", prompt)
        self.assertNotIn("\n\n\n", prompt)
