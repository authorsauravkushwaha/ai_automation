"""Tests for the pure-logic core of WebAutomation (audit finding B1).

`web_automation` was at 10% coverage despite containing the form parser, the
profile-mapping heuristics and the JS generator -- i.e. all of the code that
decides *what value gets typed into which field*. None of that needs a browser,
so it is all covered here: HTML parsing, canonical-field inference, mapping,
plan building, JS generation and URL policy.
"""

from __future__ import annotations

import unittest

from hinata.automation.app_control import AutomationError
from hinata.automation.web_automation import (
    FormInspector,
    build_fill_plan,
    generate_js_snippet,
    heuristic_canonical_for_field,
    inspect_html,
    map_fields_to_profile,
    normalize_url,
)

SIMPLE_FORM = """
<html><body>
<form action="/signup" method="post" id="signup" class="wide">
  <label for="first">First name</label>
  <input id="first" name="first_name" type="text" placeholder="Jane">
  <label for="mail">Email</label>
  <input id="mail" name="email" type="email" required>
  <label for="pw">Password</label>
  <input id="pw" name="password" type="password">
  <select id="country" name="country">
    <option value="us">United States</option>
    <option value="in">India</option>
  </select>
  <textarea id="bio" name="bio"></textarea>
  <input type="checkbox" id="tos" name="tos">
  <button type="submit">Create account</button>
</form>
</body></html>
"""


class NormalizeUrlTests(unittest.TestCase):
    def test_bare_domain_gets_https(self) -> None:
        self.assertEqual(normalize_url("example.com"), "https://example.com")
        self.assertEqual(normalize_url("www.example.com/path"), "https://www.example.com/path")

    def test_surrounding_quotes_and_angle_brackets_are_stripped(self) -> None:
        self.assertEqual(normalize_url('  "<https://example.com>"  '), "https://example.com")

    def test_empty_url_is_rejected(self) -> None:
        with self.assertRaises(AutomationError):
            normalize_url("   ")

    def test_non_http_schemes_are_rejected(self) -> None:
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://example.com",
                    "data:text/html,<script>alert(1)</script>"):
            with self.subTest(url=bad), self.assertRaises(AutomationError):
                normalize_url(bad)

    def test_mdns_local_is_blocked_by_default(self) -> None:
        with self.assertRaises(AutomationError):
            normalize_url("http://printer.local")
        self.assertEqual(
            normalize_url("http://printer.local", allow_private=True), "http://printer.local"
        )

    def test_private_ranges_are_blocked_by_default(self) -> None:
        for host in ("10.0.0.5", "192.168.1.1", "172.16.0.1"):
            with self.subTest(host=host), self.assertRaises(AutomationError):
                normalize_url(f"http://{host}/admin")

    def test_public_ip_lookalike_domains_are_allowed(self) -> None:
        # "192.com" and "10.com" are real registrable domains, not addresses.
        self.assertEqual(normalize_url("https://192.com"), "https://192.com")


class InspectHtmlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = inspect_html(SIMPLE_FORM, base_url="https://example.com/signup")

    def test_finds_the_form_and_its_metadata(self) -> None:
        self.assertEqual(len(self.result["forms"]), 1)
        form = self.result["forms"][0]
        self.assertEqual(form["action"], "/signup")
        self.assertEqual(form["method"].lower(), "post")
        self.assertEqual(form["id"], "signup")

    def test_captures_all_field_kinds(self) -> None:
        tags = [f["tag"] for f in self.result["forms"][0]["fields"]]
        self.assertIn("input", tags)
        self.assertIn("select", tags)
        self.assertIn("textarea", tags)

    def test_select_options_are_collected(self) -> None:
        select = next(f for f in self.result["forms"][0]["fields"] if f["tag"] == "select")
        self.assertEqual([o["value"] for o in select.get("options", [])], ["us", "in"])

    def test_labels_are_attached_via_for_attribute(self) -> None:
        first = next(f for f in self.result["forms"][0]["fields"] if f.get("id") == "first")
        self.assertIn("First name", first.get("label", ""))

    def test_submit_button_is_identified(self) -> None:
        self.assertTrue(self.result["forms"][0]["buttons"])

    def test_inputs_outside_any_form_still_get_a_container(self) -> None:
        result = inspect_html('<input name="standalone" id="s1">')
        self.assertTrue(result["forms"])
        self.assertEqual(result["forms"][0]["fields"][0]["name"], "standalone")

    def test_empty_html_is_harmless(self) -> None:
        self.assertEqual(inspect_html("")["forms"], [])

    def test_malformed_html_does_not_raise(self) -> None:
        result = inspect_html("<form><input name='a'><div><label>oops</form></div>")
        self.assertIsInstance(result["forms"], list)

    def test_relative_action_is_preserved_verbatim(self) -> None:
        # The plan must not silently rewrite a form target; callers resolve it.
        result = inspect_html('<form action="/login"></form>', base_url="https://example.com/a/b")
        self.assertEqual(result["forms"][0]["action"], "/login")

    def test_form_inspector_is_a_real_html_parser(self) -> None:
        inspector = FormInspector(base_url="https://e.com")
        inspector.feed(SIMPLE_FORM)
        inspector.close()
        self.assertEqual(len(inspector.forms), 1)


class CanonicalInferenceTests(unittest.TestCase):
    def test_common_fields_are_inferred(self) -> None:
        cases = [
            ({"name": "email", "id": "email"}, "email"),
            ({"name": "first_name"}, "first_name"),
            ({"name": "last_name"}, "last_name"),
            ({"name": "phone"}, "phone"),
        ]
        for field, expected in cases:
            with self.subTest(field=field):
                canonical, confidence, _reason = heuristic_canonical_for_field(field)
                self.assertEqual(canonical, expected)
                self.assertGreater(confidence, 0)

    def test_password_is_never_a_canonical_fill_target(self) -> None:
        """Passwords are not autofill data: they must not be inferred as a target."""
        canonical, _c, _r = heuristic_canonical_for_field({"name": "password", "type": "password"})
        self.assertIsNone(canonical)

    def test_unknown_field_is_not_guessed(self) -> None:
        canonical, confidence, _reason = heuristic_canonical_for_field({"name": "xyzzy_42"})
        self.assertIsNone(canonical)
        self.assertEqual(confidence, 0)

    def test_confidence_is_a_fraction_not_a_raw_weight(self) -> None:
        for name in ("email", "name", "phone", "address", "unknown_thing"):
            _c, confidence, _r = heuristic_canonical_for_field({"name": name})
            self.assertGreaterEqual(confidence, 0.0)
            self.assertLessEqual(confidence, 1.0, f"{name} confidence out of range")

    def test_stronger_signal_gets_higher_confidence(self) -> None:
        _c, by_name, _ = heuristic_canonical_for_field({"name": "email"})
        _c, by_placeholder, _ = heuristic_canonical_for_field({"placeholder": "email"})
        self.assertGreater(by_name, by_placeholder)


class MapFieldsTests(unittest.TestCase):
    def test_profile_values_are_mapped_onto_fields(self) -> None:
        fields = inspect_html(SIMPLE_FORM)["forms"][0]["fields"]
        mapped = map_fields_to_profile(
            fields, {"email": "jane@example.com", "first_name": "Jane"}
        )
        by_canonical = {m["canonical"]: m for m in mapped if m.get("canonical")}
        self.assertEqual(by_canonical["email"]["value"], "jane@example.com")
        self.assertEqual(by_canonical["first_name"]["value"], "Jane")

    def test_fields_without_a_profile_value_map_to_none(self) -> None:
        fields = inspect_html(SIMPLE_FORM)["forms"][0]["fields"]
        mapped = map_fields_to_profile(fields, {})
        self.assertTrue(all(m.get("value") is None for m in mapped))

    def test_password_is_not_taken_from_a_plain_profile(self) -> None:
        """A saved credential must never be auto-typed into an arbitrary page."""
        for key in ("password", "pin", "otp"):
            with self.subTest(field=key):
                fields = [{"tag": "input", "name": key, "id": key, "type": "text"}]
                mapped = map_fields_to_profile(fields, {key: "hunter2"})
                self.assertNotIn("hunter2", [m.get("value") for m in mapped])


class BuildPlanTests(unittest.TestCase):
    def _plan(self, profile: dict[str, str], *, submit: bool = False):
        inspection = inspect_html(SIMPLE_FORM, base_url="https://example.com/signup")
        return build_fill_plan(inspection, profile, submit=submit)

    def test_plan_prefers_id_selector(self) -> None:
        plan = self._plan({"email": "jane@example.com"})
        action = next(a for a in plan.actions if a["canonical"] == "email")
        self.assertEqual(action["selector"], "#mail")

    def test_plan_counts(self) -> None:
        plan = self._plan({"email": "jane@example.com", "first_name": "Jane"})
        self.assertGreaterEqual(plan.mappable, 2)
        self.assertEqual(plan.total_fields, len(
            inspect_html(SIMPLE_FORM)["forms"][0]["fields"]
        ))

    def test_no_actions_without_profile_data(self) -> None:
        self.assertEqual(self._plan({}).actions, [])

    def test_submit_selector_only_when_requested(self) -> None:
        profile = {"email": "jane@example.com"}
        self.assertIsNone(self._plan(profile, submit=False).submit_selector)
        # A submit selector is offered for review, never auto-clicked by the plan.
        self.assertTrue(self._plan(profile, submit=True).submit_selector)


class JsSnippetTests(unittest.TestCase):
    def test_snippet_targets_the_planned_selectors(self) -> None:
        plan = build_fill_plan(
            inspect_html(SIMPLE_FORM), {"email": "jane@example.com"}, submit=False
        )
        snippet = generate_js_snippet(plan)
        self.assertIn("#mail", snippet)
        self.assertIn("jane@example.com", snippet)

    def test_snippet_escapes_quote_characters(self) -> None:
        """A profile value containing quotes must not break out of the JS string."""
        plan = build_fill_plan(
            inspect_html(SIMPLE_FORM),
            {"first_name": 'Ev"il\'}); alert(1); //'},
            submit=False,
        )
        snippet = generate_js_snippet(plan)
        self.assertNotIn('"Ev"il', snippet)
        self.assertNotIn("alert(1); //'", snippet.split("const value")[0])

    def test_snippet_with_no_actions_still_valid_js(self) -> None:
        plan = build_fill_plan(inspect_html(SIMPLE_FORM), {})
        snippet = generate_js_snippet(plan)
        self.assertIsInstance(snippet, str)
        self.assertTrue(snippet.strip())

    def test_snippet_never_contains_a_raw_password(self) -> None:
        plan = build_fill_plan(inspect_html(SIMPLE_FORM), {"password": "hunter2"})
        self.assertNotIn("hunter2", generate_js_snippet(plan))

    def test_snippet_cannot_close_an_inline_script_tag(self) -> None:
        """json.dumps leaves </script> intact, which breaks out of <script>."""
        plan = build_fill_plan(
            inspect_html(SIMPLE_FORM),
            {"first_name": "Eve</script><img src=x onerror=alert(1)>"},
        )
        snippet = generate_js_snippet(plan)
        self.assertNotIn("</script>", snippet)
        self.assertIn("<\\/script>", snippet)

    def test_snippet_escapes_unicode_line_separators(self) -> None:
        plan = build_fill_plan(inspect_html(SIMPLE_FORM), {"first_name": "a\u2028b"})
        self.assertIn("\\u2028", generate_js_snippet(plan))


if __name__ == "__main__":
    unittest.main()


class FormInspectorEdgeCaseTests(unittest.TestCase):
    """The parser has to cope with the HTML that real pages actually serve."""

    def test_wrapping_label_attaches_to_the_enclosed_field(self) -> None:
        result = inspect_html('<form><label>Email address <input name="email"></label></form>')
        field = result["forms"][0]["fields"][0]
        self.assertIn("Email address", field.get("label", ""))

    def test_label_after_field_is_attached_retroactively(self) -> None:
        result = inspect_html(
            '<form><input id="city"><label for="city">City</label></form>'
        )
        self.assertEqual(result["forms"][0]["fields"][0]["label"], "City")

    def test_nested_forms_do_not_lose_fields(self) -> None:
        result = inspect_html(
            '<form id="outer"><input name="a"><form id="inner"><input name="b"></form></form>'
        )
        names = [f["name"] for form in result["forms"] for f in form["fields"]]
        self.assertIn("a", names)
        self.assertIn("b", names)

    def test_button_submit_and_image_inputs_become_buttons(self) -> None:
        result = inspect_html(
            '<form><input type="image" name="go" value="Go"><button name="s">Send</button></form>'
        )
        self.assertTrue(result["forms"][0]["buttons"])

    def test_required_and_autocomplete_attributes_survive(self) -> None:
        result = inspect_html(
            '<form><input name="email" autocomplete="email" required></form>'
        )
        field = result["forms"][0]["fields"][0]
        self.assertTrue(field["required"])
        self.assertEqual(field["autocomplete"], "email")

    def test_aria_labelledby_is_used_as_a_signal(self) -> None:
        result = inspect_html(
            '<form><span id="lbl">Email</span>'
            '<input name="x" aria-labelledby="lbl"></form>'
        )
        self.assertEqual(result["forms"][0]["fields"][0]["aria_label"], "lbl")

    def test_datalist_is_ignored_without_error(self) -> None:
        result = inspect_html(
            '<form><input name="email" list="opts"><datalist id="opts">'
            "<option value='a@b.c'></datalist></form>"
        )
        self.assertEqual(len(result["forms"][0]["fields"]), 1)

    def test_unclosed_form_does_not_break_later_parsing(self) -> None:
        result = inspect_html("<form><input name='a'><input name='b'>")
        self.assertEqual(len(result["forms"][0]["fields"]), 2)


class AutocompleteSignalTests(unittest.TestCase):
    def test_autocomplete_token_gives_full_confidence(self) -> None:
        for hint in ("email", "tel", "given-name", "family-name", "postal-code"):
            with self.subTest(hint=hint):
                canonical, confidence, signal = heuristic_canonical_for_field(
                    {"autocomplete": hint}
                )
                self.assertIsNotNone(canonical)
                self.assertEqual(confidence, 1.0)
                self.assertEqual(signal, "autocomplete")

    def test_multi_token_autocomplete_uses_the_last_meaningful_token(self) -> None:
        canonical, _c, _s = heuristic_canonical_for_field(
            {"autocomplete": "section-checkout shipping given-name"}
        )
        self.assertEqual(canonical, "first_name")

    def test_unknown_autocomplete_falls_through_to_other_signals(self) -> None:
        canonical, _c, _s = heuristic_canonical_for_field(
            {"autocomplete": "off", "name": "email"}
        )
        self.assertEqual(canonical, "email")

    def test_type_boost_raises_confidence_but_not_above_one(self) -> None:
        _c, plain, _ = heuristic_canonical_for_field({"name": "email"})
        _c, boosted, _ = heuristic_canonical_for_field({"name": "email", "type": "email"})
        self.assertGreater(boosted, plain)
        self.assertLessEqual(boosted, 1.0)


class FuzzyFallbackTests(unittest.TestCase):
    def test_unknown_field_can_fuzzy_match_a_profile_key(self) -> None:
        # "nickname" is not a canonical field, so only the last-ditch branch can map it.
        mapped = map_fields_to_profile(
            [{"tag": "input", "name": "my_nickname_field"}], {"nickname": "Zed"}
        )
        self.assertEqual(mapped[0]["value"], "Zed")
        self.assertIn("fuzzy", mapped[0]["signal"])

    def test_compound_name_matches_through_the_substring_rule(self) -> None:
        mapped = map_fields_to_profile(
            [{"tag": "input", "name": "customer_email_address"}], {"email": "a@b.c"}
        )
        self.assertEqual(mapped[0]["value"], "a@b.c")

    def test_fuzzy_match_confidence_is_below_exact_match(self) -> None:
        fuzzy = map_fields_to_profile(
            [{"tag": "input", "name": "my_nickname_field"}], {"nickname": "Zed"}
        )[0]["confidence"]
        exact = map_fields_to_profile([{"tag": "input", "name": "email"}], {"email": "a@b.c"})[0][
            "confidence"
        ]
        self.assertLess(fuzzy, exact)

    def test_no_profile_key_match_leaves_field_unmapped(self) -> None:
        self.assertEqual(
            map_fields_to_profile([{"tag": "input", "name": "qqqzzz"}], {"email": "a@b.c"}),
            [],
        )


class SensitiveValuePreviewTests(unittest.TestCase):
    def test_sensitive_values_are_masked_in_the_log_payload(self) -> None:
        mapped = map_fields_to_profile(
            [{"tag": "input", "name": "pan", "id": "pan"}],
            {"pan": "ABCDE1234F"},
        )
        self.assertTrue(mapped)
        self.assertNotIn("ABCDE1234F", str(mapped[0]["value_preview"]))
