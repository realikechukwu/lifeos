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
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from assistant.agent.loop import READ_ONLY_TOOLS, _run_tool
from assistant.agent.tools import ACTIVE_TOOL_DEFINITIONS, HANDLERS, TOOL_DEFINITIONS, ToolContext, ToolInputError
from assistant.services import homebase

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
        self.sign_ins = 0
        self.reject_next_token = False

    def __call__(self, request, timeout=None):
        url = urlsplit(request.full_url)
        self.requests.append(request)
        if url.path == "/auth/v1/token":
            self.sign_ins += 1
            return FakeResponse(json.dumps({"access_token": f"token-{self.sign_ins}", "expires_in": 3600}).encode())
        if self.reject_next_token:
            self.reject_next_token = False
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        if url.path.startswith("/rest/v1/"):
            return FakeResponse(json.dumps(self.tables[url.path.removeprefix("/rest/v1/")]).encode())
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
        self._send(b'{"access_token": "token-1", "expires_in": 3600}')

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

    def test_tools_are_registered_read_only_and_off_by_default(self):
        self.assertTrue(TOOL_NAMES <= {t["name"] for t in TOOL_DEFINITIONS})
        self.assertTrue(TOOL_NAMES <= set(HANDLERS))
        self.assertTrue(TOOL_NAMES <= READ_ONLY_TOOLS)
        # The test environment starts with AGENT_HOMEBASE_ENABLED unset.
        self.assertFalse(TOOL_NAMES & {t["name"] for t in ACTIVE_TOOL_DEFINITIONS})

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
