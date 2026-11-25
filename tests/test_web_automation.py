"""Tests for HINATA Web Automation — open, fill, submit any website."""

from __future__ import annotations

import http.server
import pathlib
import tempfile
import threading

from hinata.automation.app_control import AppControl
from hinata.automation.file_manager import FileManager
from hinata.automation.jarvis_cockpit import JarvisOverrideController
from hinata.automation.system_info import SystemInfo
from hinata.automation.web_automation import (
    WebAutomation,
    heuristic_canonical_for_field,
    inspect_html,
    map_fields_to_profile,
    normalize_url,
)
from hinata.automation.windows_control import WindowsControl
from hinata.config import HinataConfig
from hinata.core.identity_vault import CANONICAL_FIELDS, IdentityVault
from hinata.core.intent_parser import IntentParser
from hinata.core.llm_manager import LLMManager
from hinata.core.memory import Memory
from hinata.core.ollama_service import OllamaService
from hinata.core.orchestrator import Orchestrator
from hinata.core.permissions import PermissionManager
from hinata.core.personality import PersonalityManager
from hinata.core.planner import Planner
from hinata.database.database import Database
from hinata.security.action_validator import ActionValidator
from hinata.security.authentication import Authentication


def _make_orch():
    cfg = HinataConfig(data_dir=pathlib.Path(tempfile.mkdtemp()))
    cfg.validate()
    db = Database(cfg.database_path)
    mem = Memory(db, 100)
    validator = ActionValidator()
    parser = IntentParser(validator)
    planner = Planner(validator)
    auth = Authentication(db)
    perm = PermissionManager(auth, override_controller=JarvisOverrideController())
    files = FileManager(cfg.security)
    vault = IdentityVault(db)
    wa = WebAutomation(files=files, windows=WindowsControl())
    llm = LLMManager(cfg.ollama, OllamaService(cfg.ollama))
    orch = Orchestrator(
        validator=validator, parser=parser, planner=planner, llm=llm, memory=mem,
        personality=PersonalityManager(db, "friend"), permissions=perm,
        apps=AppControl({}), files=files, windows=WindowsControl(), system=SystemInfo(),
        llm_enabled=False, web_automation=wa, identity_vault=vault,
    )
    return orch, vault, wa, parser, validator


def test_identity_vault_crud():
    _, vault, _, _, _ = _make_orch()
    # save + auto full_name
    p = vault.save_profile("personal", {"first_name": "Saurav", "last_name": "Kushwaha", "email": "s@example.com"})
    assert p.fields["full_name"] == "Saurav Kushwaha"
    assert vault.get_profile("personal").fields["email"] == "s@example.com"
    vault.save_profile("work", {"company": "AI Lab", "email": "w@lab.com"})
    assert len(vault.list_profiles()) == 2
    vault.set_default("work")
    assert vault.get_default().name == "work"
    vault.update_field("personal", "city", "Kolkata")
    assert vault.get_profile("personal").fields["city"] == "Kolkata"
    assert vault.delete_profile("work") is True
    assert len(vault.list_profiles()) == 1


def test_canonical_aliases():
    for k, meta in CANONICAL_FIELDS.items():
        from hinata.core.identity_vault import canonicalize_field
        assert canonicalize_field(k) == k
        # Spot-check a few aliases (some overlap is intentional, e.g. country_code vs phone_country_code)
        if k == "first_name" and "fname" in meta.get("aliases", []):
            assert canonicalize_field("fname") == "first_name"
        if k == "email":
            assert canonicalize_field("email_address") == "email"


def test_normalize_url():
    assert normalize_url("google.com") == "https://google.com"
    assert normalize_url("https://example.com/path") == "https://example.com/path"
    assert normalize_url("http://127.0.0.1:8000/", allow_private=True) == "http://127.0.0.1:8000/"
    try:
        normalize_url("ftp://example.com")
        raise AssertionError("should reject ftp")
    except Exception:
        pass


def test_inspect_simple_form():
    html = '<form action="/go" method="post"><input name="first_name" id="fn"><input type="email" name="email" placeholder="Email"><select name="country"><option>India</option></select><button type="submit">Go</button></form>'
    data = inspect_html(html, base_url="https://example.com")
    assert data["form_count"] == 1
    assert data["field_count"] == 3
    assert any(f["name"] == "first_name" for f in data["forms"][0]["fields"])


def test_heuristic_mapping():
    field = {"name": "first_name", "id": "fn", "placeholder": "", "label": "First Name", "autocomplete": "", "type": "text"}
    canon, score, sig = heuristic_canonical_for_field(field)
    assert canon == "first_name"
    field2 = {"name": "email", "id": "", "placeholder": "Email address", "label": "", "autocomplete": "email", "type": "email"}
    canon2, _, _ = heuristic_canonical_for_field(field2)
    assert canon2 == "email"
    field3 = {"name": "city", "id": "", "placeholder": "", "label": "", "autocomplete": "address-level2", "type": "text"}
    canon3, _, _ = heuristic_canonical_for_field(field3)
    assert canon3 == "city"


def test_map_fields_to_profile():
    _, vault, _, _, _ = _make_orch()
    p = vault.save_profile("personal", {"first_name": "Saurav", "last_name": "Kushwaha", "email": "s@e.com", "phone": "123"})
    html = '<form><input name="first_name"><input name="email" type="email"><input name="phone"></form>'
    data = inspect_html(html)
    fields = data["forms"][0]["fields"]
    mappings = map_fields_to_profile(fields, p.fields)
    assert len(mappings) == 3
    assert {m["canonical"] for m in mappings} == {"first_name", "email", "phone"}


def test_intent_parser_web_automation():
    _, _, _, parser, _ = _make_orch()
    assert parser.parse("open website google.com").tool == "open_website"
    assert parser.parse("go to https://example.com").tool == "open_website"
    assert parser.parse("inspect forms on https://example.com").tool == "inspect_forms"
    assert parser.parse("save my identity as personal: first_name=John, email=john@x.com").tool == "save_identity"
    assert parser.parse("list my identities").tool == "list_identities"
    assert parser.parse("show my profile").tool == "get_identity"
    assert parser.parse("autofill https://example.com with personal").tool == "web_autofill"
    assert parser.parse("fill form https://example.com").tool == "web_autofill"
    assert parser.parse("submit form https://example.com").tool == "web_autofill"


def test_validation_new_tools():
    v = ActionValidator()
    v.validate("open_website", {"url": "https://example.com"})
    v.validate("inspect_forms", {"url": "https://example.com"})
    v.validate("save_identity", {"profile": "personal", "fields_json": '{"a":"b"}'})
    v.validate("web_autofill", {"url": "https://example.com"})
    v.validate("web_fill_field", {"url": "https://example.com", "selector": "#a", "value": "hi"})
    v.validate("web_click", {"url": "https://example.com", "selector": "#a"})


def test_orchestrator_save_and_autofill_via_local_server():
    orch, _, _, _, _ = _make_orch()
    from hinata.core.types import ActionRequest
    # save
    r = orch._run_tool(ActionRequest(tool="save_identity", arguments={"profile": "personal", "fields_json": '{"first_name":"Saurav","last_name":"Kushwaha","email":"s@e.com","phone":"9876543210","city":"Kolkata"}'}))
    assert r.success
    # start server
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><form action='/ok'><input name='first_name'><input name='last_name'><input name='email' type='email'><input name='phone'><button type='submit'>Submit</button></form></html>")
        def log_message(self, *a, **kw): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    thr = threading.Thread(target=srv.serve_forever, daemon=True)
    thr.start()
    try:
        r2 = orch._run_tool(ActionRequest(tool="inspect_forms", arguments={"url": f"http://127.0.0.1:{port}/"}))
        assert r2.success
        assert r2.data["field_count"] == 4
        r3 = orch._run_tool(ActionRequest(tool="web_autofill", arguments={"url": f"http://127.0.0.1:{port}/", "profile": "personal"}))
        assert r3.success
        assert r3.data["plan"]["mappable"] == 4
        assert "js_snippet" in r3.data
        assert "Saurav" in r3.data["js_snippet"]
        # with submit
        r4 = orch._run_tool(ActionRequest(tool="web_autofill", arguments={"url": f"http://127.0.0.1:{port}/", "profile": "personal", "submit": True}))
        assert r4.success
        assert r4.data["plan"]["submit_selector"] is not None
    finally:
        srv.shutdown()


def test_payment_fields_block_submit():
    orch, _, _, _, _ = _make_orch()
    from hinata.core.types import ActionRequest
    orch._run_tool(ActionRequest(tool="save_identity", arguments={"profile": "personal", "fields_json": '{"first_name":"A","card_number":"4111111111111111","card_cvv":"123","email":"a@b.com"}'}))
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><form><input name='first_name'><input name='card_number'><input name='card_cvv'><button type='submit'>Pay</button></form></html>")
        def log_message(self, *a, **kw): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    thr = threading.Thread(target=srv.serve_forever, daemon=True)
    thr.start()
    try:
        r = orch._run_tool(ActionRequest(tool="web_autofill", arguments={"url": f"http://127.0.0.1:{port}/", "profile": "personal", "submit": True}))
        assert r.success
        # Payment should be flagged and submit disabled via warning, not actually submitted with driver (native won't submit anyway)
        assert "Payment" in r.message or "payment" in r.message.lower()
    finally:
        srv.shutdown()


def test_open_website_headless():
    orch, _, _, _, _ = _make_orch()
    from hinata.core.types import ActionRequest
    r = orch._run_tool(ActionRequest(tool="open_website", arguments={"url": "https://example.com", "headless": True}))
    assert r.success
    assert r.data["browser"] == "headless"
