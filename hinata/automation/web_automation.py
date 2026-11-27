"""HINATA Web Automation Engine — open, fill and submit any website.

This is the core that makes Hinata "so strong" for form filling:

- Open any URL (normalizes bare domains, validates SSRF-safe URL, picks best browser)
- Inspect any page for forms (std-lib HTMLParser, zero mandatory deps)
- Intelligent field→identity mapping (50+ canonical fields, alias-aware heuristics)
- Multi-backend execution:
    1) Playwright (if installed) — most reliable, handles SPA, shadow-dom, waits
    2) Selenium (if installed) — fallback driver
    3) Native browser open + Windows type_text / assistive JS snippet — always available

All actions are owner-approved (CONFIRM) and non-destructive.
No password/token is ever auto-submitted without explicit confirmation.

Architecture: single class WebAutomation with pure helpers extracted for testability.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.security.net_guards import build_public_web_opener

try:
    from hinata.automation.browser_search import open_browser_search  # type: ignore
except Exception:  # pragma: no cover
    open_browser_search = None  # type: ignore

LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------- URL helpers


def _url_policy(url: str, *, resolve: bool, allow_loopback: bool = True) -> bool:
    """Use the web-search SSRF policy without importing it at module load time."""
    from hinata.automation.web_search import is_safe_public_url

    return is_safe_public_url(url, resolve=resolve, allow_loopback=allow_loopback)


def ensure_public_fetch_url(url: str, *, allow_loopback: bool = True) -> None:
    """Reject a fetch target unless it is public (or an explicit loopback literal)."""
    if not _url_policy(url, resolve=True, allow_loopback=allow_loopback):
        raise AutomationError(f"Refused non-public or invalid URL: {url}")


def normalize_url(raw: str, *, allow_private: bool = False) -> str:
    raw = raw.strip().strip("<>\"'").strip()
    if not raw:
        raise AutomationError("URL cannot be empty")
    # If user typed google.com or www.example.com, add https
    if re.match(r"^[a-zA-Z0-9.-]+\.[a-z]{2,}(/.*)?$", raw) and "://" not in raw:
        raw = "https://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise AutomationError(f"Only http(s) URLs are supported, got: {parsed.scheme or raw[:40]}")
    if not allow_private and not _url_policy(raw, resolve=False, allow_loopback=True):
        # Literal 127.0.0.0/8 and localhost remain usable for local development;
        # private ranges, metadata IPs, mDNS names, URL credentials and numeric
        # IP spellings are refused. DNS-backed resolution happens before fetching.
        raise AutomationError(f"Refused non-public or invalid URL: {raw}")
    return raw


# --------------------------------------------------------------------------- Form parser (stdlib only)


class FormInspector(HTMLParser):
    """Extract every fillable field + form structure from raw HTML."""

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.forms: list[dict[str, Any]] = []
        self._form_stack: list[dict[str, Any]] = []
        self._label_for: dict[str, str] = {}
        self._pending_label: str | None = None
        self._in_label = False
        self._label_buf: list[str] = []
        self._in_button = False
        self._button_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        amap = {k.lower(): (v or "") for k, v in attrs}
        if t == "form":
            form: dict[str, Any] = {
                "action": amap.get("action", ""),
                "method": (amap.get("method", "get") or "get").lower().strip() or "get",
                "id": amap.get("id", ""),
                "name": amap.get("name", ""),
                "class": amap.get("class", ""),
                "fields": [],
                "buttons": [],
            }
            self.forms.append(form)
            self._form_stack.append(form)
        elif t == "label":
            self._in_label = True
            self._label_buf = []
            self._pending_label = amap.get("for", "") or None  # None means wrap-label
        elif t == "input":
            itype = (amap.get("type", "text") or "text").lower().strip()
            # Skip non-fillable
            if itype in {"submit", "button", "image", "reset", "hidden"}:
                # Hidden still matters for submission but not autofill - record separately
                field = {
                    "tag": "input",
                    "type": itype,
                    "name": amap.get("name", ""),
                    "id": amap.get("id", ""),
                    "placeholder": amap.get("placeholder", ""),
                    "autocomplete": amap.get("autocomplete", ""),
                    "aria_label": amap.get("aria-label", "") or amap.get("aria-labelledby", ""),
                    "value": amap.get("value", ""),
                    "required": "required" in amap,
                    "label": "",
                    "form_index": len(self.forms) - 1 if self.forms else -1,
                }
                target_form = self._form_stack[-1] if self._form_stack else None
                if target_form is None:
                    # orphan field — create synthetic form entry
                    if not self.forms or self.forms[-1].get("_synthetic") is not True:
                        self.forms.append({"action": "", "method": "get", "id": "", "name": "", "class": "", "fields": [], "buttons": [], "_synthetic": True})
                    target_form = self.forms[-1]
                target_form["fields"].append(field)
                target_form["buttons"].append({"type": itype, "value": amap.get("value", ""), "name": amap.get("name", "")}) if itype in {"submit", "image"} else None
                return
            field = {
                "tag": "input",
                "type": itype,
                "name": amap.get("name", ""),
                "id": amap.get("id", ""),
                "placeholder": amap.get("placeholder", ""),
                "autocomplete": amap.get("autocomplete", ""),
                "aria_label": amap.get("aria-label", "") or amap.get("aria-labelledby", ""),
                "value": amap.get("value", ""),
                "pattern": amap.get("pattern", ""),
                "required": "required" in amap,
                "label": "",
                "form_index": len(self.forms) - 1 if self.forms else -1,
            }
            # Attach label if we have for->id mapping
            fid = field["id"]
            if fid and fid in self._label_for:
                field["label"] = self._label_for[fid]
            elif self._in_label and self._pending_label is None:
                # wrap-label will be attached on label close via buffer — we stash
                field["_wrap_label_pending"] = True
            target_form = self._form_stack[-1] if self._form_stack else None
            if target_form is None:
                if not self.forms or self.forms[-1].get("_synthetic") is not True:
                    self.forms.append({"action": "", "method": "get", "id": "", "name": "", "class": "", "fields": [], "buttons": [], "_synthetic": True})
                target_form = self.forms[-1]
            target_form["fields"].append(field)
        elif t in {"textarea", "select"}:
            field = {
                "tag": t,
                "type": t,
                "name": amap.get("name", ""),
                "id": amap.get("id", ""),
                "placeholder": amap.get("placeholder", ""),
                "autocomplete": amap.get("autocomplete", ""),
                "aria_label": amap.get("aria-label", "") or amap.get("aria-labelledby", ""),
                "value": "",
                "required": "required" in amap,
                "label": "",
                "options": [] if t == "select" else None,
                "form_index": len(self.forms) - 1 if self.forms else -1,
            }
            fid = field["id"]
            if fid and fid in self._label_for:
                field["label"] = self._label_for[fid]
            target_form = self._form_stack[-1] if self._form_stack else None
            if target_form is None:
                if not self.forms or self.forms[-1].get("_synthetic") is not True:
                    self.forms.append({"action": "", "method": "get", "id": "", "name": "", "class": "", "fields": [], "buttons": [], "_synthetic": True})
                target_form = self.forms[-1]
            target_form["fields"].append(field)
        elif t == "option" and self.forms and self.forms[-1]["fields"] and self.forms[-1]["fields"][-1]["tag"] == "select":
            # Store option for select
            self.forms[-1]["fields"][-1].setdefault("options", []).append({"value": amap.get("value", ""), "text": "", "_collect": True})
        elif t == "button":
            self._in_button = True
            self._button_buf = []
            btype = (amap.get("type", "submit") or "submit").lower()
            btn = {"tag": "button", "type": btype, "text": "", "name": amap.get("name", ""), "id": amap.get("id", ""), "value": amap.get("value", "")}
            target_form = self._form_stack[-1] if self._form_stack else None
            if target_form is None:
                if not self.forms or self.forms[-1].get("_synthetic") is not True:
                    self.forms.append({"action": "", "method": "get", "id": "", "name": "", "class": "", "fields": [], "buttons": [], "_synthetic": True})
                target_form = self.forms[-1]
            target_form["buttons"].append(btn)
            target_form["_current_button"] = btn
        elif t == "datalist":
            pass  # ignore for now

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "form" and self._form_stack:
            self._form_stack.pop()
        elif t == "label":
            text = " ".join("".join(self._label_buf).split()).strip()
            if self._pending_label:
                self._label_for[self._pending_label] = text
                # Retroactively attach to existing fields with that id
                for form in self.forms:
                    for f in form["fields"]:
                        if f.get("id") == self._pending_label and not f.get("label"):
                            f["label"] = text
            else:
                # wrap-label -> attach to last field that had _wrap_label_pending
                for form in reversed(self.forms):
                    for f in reversed(form["fields"]):
                        if f.pop("_wrap_label_pending", False):
                            f["label"] = text
                            break
                    if any(f.get("label") == text for f in form["fields"]):
                        break
            self._in_label = False
            self._label_buf = []
            self._pending_label = None
        elif t == "button":
            text = " ".join("".join(self._button_buf).split()).strip()
            for form in reversed(self.forms):
                btn = form.get("_current_button")
                if btn is not None:
                    btn["text"] = text
                    form.pop("_current_button", None)
                    break
            self._in_button = False
            self._button_buf = []
        elif t == "select":
            # clear collect flag
            for form in self.forms:
                for f in form["fields"]:
                    if f.get("tag") == "select":
                        for opt in f.get("options", []) or []:
                            opt.pop("_collect", None)

    def handle_data(self, data: str) -> None:
        if self._in_label:
            self._label_buf.append(data)
        if self._in_button:
            self._button_buf.append(data)
        # Collect option text
        if self.forms and self.forms[-1]["fields"]:
            last = self.forms[-1]["fields"][-1]
            if last.get("tag") == "select" and last.get("options"):
                for opt in last["options"]:
                    if opt.get("_collect"):
                        opt["text"] = (opt.get("text", "") + data).strip()
                        break


def inspect_html(html: str, base_url: str = "") -> dict[str, Any]:
    parser = FormInspector(base_url=base_url)
    parser.feed(html)
    # Clean synthetic marker
    for form in parser.forms:
        form.pop("_synthetic", None)
        form.pop("_current_button", None)
    # Enrich with selectors (what Playwright/Selenium would target)
    for fi, form in enumerate(parser.forms):
        for field in form["fields"]:
            selectors = []
            if field.get("id"):
                selectors.append(f"#{field['id']}")
            if field.get("name"):
                selectors.append(f"[name='{field['name']}']")
            if field.get("placeholder"):
                selectors.append(f"[placeholder='{field['placeholder']}']")
            field["selectors"] = selectors
            field["form_index"] = fi
    total_fields = sum(len(f["fields"]) for f in parser.forms)
    return {
        "url": base_url,
        "forms": parser.forms,
        "form_count": len(parser.forms),
        "field_count": total_fields,
        "has_forms": total_fields > 0,
    }


# --------------------------------------------------------------------------- Intelligent mapping heuristics

# Additional browser-autofill hints (autocomplete attribute values -> canonical)
AUTOCOMPLETE_MAP: dict[str, str] = {
    "name": "full_name",
    "given-name": "first_name",
    "family-name": "last_name",
    "additional-name": "middle_name",
    "honorific-prefix": "title",
    "honorific-suffix": "suffix",
    "email": "email",
    "tel": "phone",
    "tel-country-code": "phone_country_code",
    "street-address": "address_line1",
    "address-line1": "address_line1",
    "address-line2": "address_line2",
    "address-level2": "city",
    "address-level1": "state",
    "postal-code": "zip",
    "country": "country",
    "country-name": "country",
    "bday": "date_of_birth",
    "bday-day": "date_of_birth",
    "bday-month": "date_of_birth",
    "bday-year": "date_of_birth",
    "sex": "gender",
    "organization": "company",
    "organization-title": "job_title",
    "url": "website",
    "cc-name": "card_holder",
    "cc-number": "card_number",
    "cc-exp": "card_expiry",
    "cc-csc": "card_cvv",
}

# Heuristic weights for text signals
#: Relative trust in each HTML signal. Raw weights are normalised to 0..1 before
#: leaving ``heuristic_canonical_for_field``: callers treat "confidence" as a
#: probability, and a raw weight of 8 (or 20 after the type boost) is not one.
SIGNAL_WEIGHTS = {"autocomplete": 10, "name": 8, "id": 7, "label": 6, "placeholder": 5, "aria_label": 4}

#: Highest score the loop can produce: the largest weight, doubled by the
#: autocomplete direct-map branch.
MAX_CANONICAL_SCORE = max(SIGNAL_WEIGHTS.values()) * 2


def heuristic_canonical_for_field(field: dict[str, Any]) -> tuple[str | None, float, str]:
    """Return (canonical, score, signal_used) for a single HTML field dict."""
    # Deferred import to avoid cycle
    from hinata.core.identity_vault import canonicalize_field, normalize_key

    best: str | None = None
    best_score = 0.0
    best_signal = ""
    for signal, weight in SIGNAL_WEIGHTS.items():
        raw = (field.get(signal) or "").strip()
        if not raw:
            continue
        # Handle autocomplete with direct map
        if signal == "autocomplete":
            # autocomplete can be space-separated tokens; take last token
            tokens = raw.lower().split()
            for token in reversed(tokens):
                direct = AUTOCOMPLETE_MAP.get(token)
                if direct:
                    return direct, 1.0, signal  # strongest signal: a declared hint
                canon = canonicalize_field(token)
                if canon:
                    return canon, 1.0, signal
        canon = canonicalize_field(raw)
        if canon:
            # Boost if field type matches (e.g., type=email -> email field)
            score = float(weight)
            ftype = (field.get("type") or "").lower()
            if ftype == "email" and canon == "email":
                score += 3
            elif ftype == "tel" and canon in {"phone", "phone_country_code"}:
                score += 3
            elif ftype == "date" and canon == "date_of_birth":
                score += 2
            elif ftype == "number" and canon in {"zip", "phone"}:
                score += 1
            if score > best_score:
                best = canon
                best_score = score
                best_signal = signal
        else:
            # Attempt substring match for compound names: e.g., "userEmailAddress" contains "email"
            norm = normalize_key(raw)
            # Check if any canonical alias appears inside norm
            if len(norm) >= 3:
                for cand in ("first_name", "last_name", "full_name", "email", "phone", "city", "state", "zip", "country", "address", "company", "job_title"):
                    if cand.replace("_", "") in norm.replace("_", "") or norm.replace("_", "") in cand.replace("_", ""):
                        # Lower confidence fallback
                        fallback_canon = "address_line1" if cand == "address" else cand
                        # Map address -> address_line1
                        score = float(weight) * 0.4
                        if score > best_score:
                            best = fallback_canon
                            best_score = score
                            best_signal = signal
    confidence = round(min(1.0, best_score / MAX_CANONICAL_SCORE), 2) if best else 0.0
    return best, confidence, best_signal


#: Input types that are credentials by definition.
_CREDENTIAL_INPUT_TYPES = frozenset({"password", "hidden"})


def _is_credential_field(field: dict[str, Any]) -> bool:
    """True when an HTML field must never be autofilled with stored data."""
    from hinata.core.identity_vault import _is_secret_field_name

    if (field.get("type") or "").strip().lower() in _CREDENTIAL_INPUT_TYPES:
        return True
    for signal in ("name", "id", "autocomplete", "label", "placeholder", "aria_label"):
        raw = str(field.get(signal) or "")
        if raw and _is_secret_field_name(raw):
            return True
    return False


def map_fields_to_profile(fields: list[dict[str, Any]], profile_fields: dict[str, str]) -> list[dict[str, Any]]:
    """Map parsed HTML fields to profile values with confidence scores."""
    from hinata.core.identity_vault import SENSITIVE_FIELDS

    mappings: list[dict[str, Any]] = []
    for field in fields:
        # Never type a credential, whatever the profile happens to contain and
        # however confidently the name matches: autofill is for identity data.
        # The last-ditch fuzzy match below would otherwise map a profile key
        # straight onto a password/PIN/OTP input.
        if _is_credential_field(field):
            continue
        canon, score, signal = heuristic_canonical_for_field(field)
        if not canon:
            # Last-ditch: try any profile key that appears in any signal
            for sig_key in ("name", "id", "label", "placeholder", "autocomplete"):
                raw = (field.get(sig_key) or "").lower()
                if not raw:
                    continue
                for pk in profile_fields.keys():
                    if pk.lower().replace("_", "") in raw.replace("_", "").replace("-", "") or raw.replace("_", "").replace("-", "") in pk.lower().replace("_", ""):
                        canon = pk
                        score = 0.2  # fuzzy match: below any exact-signal score
                        signal = sig_key + "+fuzzy"
                        break
                if canon:
                    break
        if canon and canon in profile_fields:
            val = profile_fields[canon]
            # Redact sensitive in log payload
            safe = ("***" + val[-4:]) if canon in SENSITIVE_FIELDS and len(val) > 4 else val if canon not in SENSITIVE_FIELDS else "***"
            mappings.append({
                "field": field,
                "canonical": canon,
                "value": val,
                "value_preview": safe,
                "confidence": round(float(score), 2),
                "signal": signal,
            })
        elif canon:
            mappings.append({
                "field": field,
                "canonical": canon,
                "value": None,
                "value_preview": None,
                "confidence": round(float(score), 2),
                "signal": signal,
                "missing_in_profile": True,
            })
    return mappings


# --------------------------------------------------------------------------- Driver abstractions


@dataclass(slots=True)
class FillPlan:
    url: str
    total_fields: int
    mappable: int
    missing: int
    actions: list[dict[str, Any]]  # each is {selector, value, canonical, confidence, action_type}
    submit_selector: str | None


def build_fill_plan(inspection: dict[str, Any], profile_fields: dict[str, str], *, submit: bool = False) -> FillPlan:
    """Turn inspection + profile into an executable plan."""
    all_fields = []
    for form in inspection["forms"]:
        all_fields.extend(form["fields"])
    mappings = map_fields_to_profile(all_fields, profile_fields)
    actions: list[dict[str, Any]] = []
    for m in mappings:
        if m.get("value") is None:
            continue
        field = m["field"]
        # Choose best selector (id > name > placeholder > css fallback)
        selector = ""
        if field.get("id"):
            selector = f"#{field['id']}"
        elif field.get("name"):
            selector = f"[name=\"{field['name']}\"]"
        elif field.get("placeholder"):
            selector = f"[placeholder=\"{field['placeholder']}\"]"
        else:
            # Fallback: use any selector from inspection
            sels = field.get("selectors") or []
            selector = sels[0] if sels else f"input[{field['tag']}]"
        actions.append({
            "selector": selector,
            "value": m["value"],
            "canonical": m["canonical"],
            "confidence": m["confidence"],
            "signal": m["signal"],
            "field_meta": {"name": field.get("name"), "id": field.get("id"), "type": field.get("type")},
            "action": "fill",
        })
    missing = len([m for m in mappings if m.get("missing_in_profile")])
    # Determine submit button selector
    submit_selector = None
    if submit:
        # Prefer first form's submit/button
        for form in inspection["forms"]:
            for btn in form.get("buttons", []):
                if btn.get("type") in {"submit", "button"} or btn.get("tag") == "button":
                    if btn.get("id"):
                        submit_selector = f"#{btn['id']}"
                        break
                    elif btn.get("name"):
                        submit_selector = f"[name=\"{btn['name']}\"]"
                        break
                    else:
                        submit_selector = "button[type='submit'], input[type='submit']"
                        break
            if submit_selector:
                break
        if not submit_selector:
            submit_selector = "button[type='submit'], input[type='submit'], button:has-text('Submit'), button:has-text('Send')"

    return FillPlan(
        url=inspection["url"],
        total_fields=len(all_fields),
        mappable=len(actions),
        missing=missing,
        actions=actions,
        submit_selector=submit_selector if submit else None,
    )


def _js_payload(value: Any) -> str:
    """Serialise ``value`` for embedding inside a JS string/expression.

    ``json.dumps`` already escapes quotes and backslashes, but it leaves
    ``</script>`` intact, which terminates an inline ``<script>`` block and lets
    the following HTML execute. Replace the sequence with its JSON-safe
    equivalent so the snippet is safe as a bookmarklet *or* as embedded script.
    """
    return (
        json.dumps(value, ensure_ascii=False, indent=2)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def generate_js_snippet(plan: FillPlan) -> str:
    """Generate a robust pure-JS snippet — works as bookmarklet, console paste, or userscript."""
    lines = [
        "// HINATA Autofill — paste into DevTools Console (F12) or save as Bookmarklet",
        "// Works on any website: Chrome, Edge, Firefox, Brave",
        "// Tip: Save as bookmark with prefix `javascript:` to reuse on any form",
        "(async function(){",
        "  const actions = " + _js_payload(plan.actions) + ";",
        "  const submitSelector = " + json.dumps(plan.submit_selector) + ";",
        "  let filled=0, failed=0;",
        "  // Deep query that pierces open shadow DOM + iframes (best-effort)",
        "  function deepQuery(sel){",
        "    try { let el=document.querySelector(sel); if(el) return el; } catch(e){}",
        "    // Search inside shadow roots",
        "    const walk=(root)=>{ for(const n of root.querySelectorAll('*')){ if(n.shadowRoot){ try{let f=n.shadowRoot.querySelector(sel); if(f) return f;}catch(e){} let r=walk(n.shadowRoot); if(r) return r; } } return null; };",
        "    let s=walk(document); if(s) return s;",
        "    // Search inside same-origin iframes",
        "    for(const fr of document.querySelectorAll('iframe')){ try{ let d=fr.contentDocument; if(d){ let e=d.querySelector(sel); if(e) return e; let r=walk(d); if(r) return r; } }catch(e){} }",
        "    return null;",
        "  }",
        "  function waitFor(sel, ms=5000){ return new Promise(res=>{ let el=deepQuery(sel); if(el){res(el); return;} let obs=new MutationObserver(()=>{ let e=deepQuery(sel); if(e){obs.disconnect(); res(e);} }); obs.observe(document.documentElement,{childList:true,subtree:true}); setTimeout(()=>{obs.disconnect(); res(deepQuery(sel));}, ms); }); }",
        "  function setValue(el, val){",
        "    if(!el) return false;",
        "    try{ el.scrollIntoView({block:'center'}); }catch(e){}",
        "    el.focus();",
        "    if(el.tagName==='SELECT'){",
        "      let matched=false;",
        "      for(const opt of el.options){ if(opt.value===val||opt.text.trim()===val||opt.text.trim().toLowerCase()===val.toLowerCase()){ el.value=opt.value; matched=true; break; } }",
        "      if(!matched && el.options.length){ el.selectedIndex=0; }",
        "    } else if(el.type==='checkbox' || el.type==='radio'){",
        "      const truthy = ['true','1','yes','on','checked', el.value].map(s=>String(s).toLowerCase());",
        "      el.checked = truthy.includes(String(val).toLowerCase());",
        "    } else {",
        "      const proto = el.tagName==='TEXTAREA'? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;",
        "      const desc = Object.getOwnPropertyDescriptor(proto, 'value');",
        "      if(desc && desc.set){ desc.set.call(el, val); } else { el.value=val; }",
        "    }",
        "    el.dispatchEvent(new Event('input', {bubbles:true}));",
        "    el.dispatchEvent(new Event('change', {bubbles:true}));",
        "    el.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true}));",
        "    el.dispatchEvent(new Event('blur', {bubbles:true}));",
        "    return true;",
        "  }",
        "  for(const a of actions){",
        "    let el = deepQuery(a.selector);",
        "    if(!el && a.field_meta?.name) el = deepQuery(`[name='${a.field_meta.name}']`);",
        "    if(!el && a.field_meta?.id) { try{ el = document.getElementById(a.field_meta.id); }catch(e){} }",
        "    if(!el) el = await waitFor(a.selector, 2000);",
        "    if(el){ filled++; setValue(el, a.value); console.log('[HINATA] filled', a.canonical, '=>', a.selector); } else { failed++; console.warn('[HINATA] not found', a.canonical, a.selector); }",
        "    await new Promise(r=>setTimeout(r, 80));",
        "  }",
        "  console.log(`[HINATA] Done: ${filled} filled, ${failed} not found (of ${actions.length}).`);",
        "  if(filled) {",
        "    const msg = `[HINATA] Autofilled ${filled} field(s).` + (failed? ` ${failed} not found — check console.` : ' Review before submitting!');",
        "    try{ alert(msg); }catch(e){ console.log(msg); }",
        "  } else if(failed) {",
        "    alert('[HINATA] No fields matched. Open console (F12) for details.');",
        "  }",
        "  // Auto-submit when requested and at least one field was filled",
        "  if(submitSelector && filled>0){",
        "    const btn = deepQuery(submitSelector);",
        "    if(btn){ console.log('[HINATA] Clicking submit:', submitSelector); btn.scrollIntoView({block:'center'}); btn.click(); }",
        "    else console.warn('[HINATA] Submit button not found:', submitSelector);",
        "  }",
        "})();",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- Main class


class WebAutomation:
    """High-level web automation with multiple execution backends."""

    def __init__(self, files=None, windows=None, vault=None) -> None:
        self.files = files
        self.windows = windows
        self.vault = vault

    # -------- open_website
    def open_website(self, url: str, *, browser: str = "default", headless: bool = False, allow_private: bool = False) -> dict[str, Any]:
        target = normalize_url(url, allow_private=allow_private)
        # headless implies we don't open a visible browser — we just validate & return
        if headless:
            return {"url": target, "browser": "headless", "opened": False, "note": "Validated URL for headless autofill; no window opened."}
        # Try to use browser_search helper for named browsers, else webbrowser
        try:
            if browser and browser.lower() not in {"default", "system", "os"} and open_browser_search is not None:
                # Use generic search with browser parameter to open URL directly via subprocess
                # Fallback: just call browser-specific launch
                # Try webbrowser's registered browser for the requested name first.
                try:
                    br = webbrowser.get(browser)
                    br.open(target, new=2)
                    return {"url": target, "browser": browser, "opened": True, "via": "webbrowser.get(browser)"}
                except webbrowser.Error:
                    pass
            # Default path: use OS default browser
            if os.name == "nt":
                try:
                    os.startfile(target)  # type: ignore
                    return {"url": target, "browser": "default", "opened": True, "via": "os.startfile"}
                except OSError:
                    pass
            ok = webbrowser.open(target, new=2)
            if ok:
                return {"url": target, "browser": browser, "opened": True, "via": "webbrowser.open"}
            raise AutomationError(f"Browser refused to open {target}")
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError(f"Could not open browser for {target}: {exc}") from exc

    # -------- fetch & inspect
    def fetch_page(
        self,
        url: str,
        *,
        max_bytes: int = 300_000,
        timeout: int = 12,
        allow_private: bool = False,
    ) -> dict[str, Any]:
        target = normalize_url(url, allow_private=allow_private)
        byte_limit = max(0, min(int(max_bytes), 300_000))
        request_timeout = max(1, min(int(timeout), 60))
        if not allow_private:
            ensure_public_fetch_url(target)

        def _check_redirect(resolved: str) -> None:
            if not allow_private:
                ensure_public_fetch_url(resolved)

        opener = (
            build_public_web_opener(_check_redirect)
            if not allow_private
            else urllib.request.build_opener()
        )
        req = urllib.request.Request(
            target,
            headers={"User-Agent": "Mozilla/5.0 HINATA Autofill/1.0"},
            method="GET",
        )
        try:
            with opener.open(req, timeout=request_timeout) as resp:
                raw = resp.read(byte_limit)
                charset = resp.headers.get_content_charset() or "utf-8"
                try:
                    html = raw.decode(charset, errors="replace")
                except (LookupError, UnicodeError):
                    html = raw.decode("utf-8", errors="replace")
                return {"url": target, "html": html, "status": getattr(resp, "status", 200), "bytes": len(raw)}
        except AutomationError:
            raise
        except urllib.error.HTTPError as exc:
            raise AutomationError(f"Page fetch failed ({exc.code} {exc.reason}) for {target}") from exc
        except urllib.error.URLError as exc:
            raise AutomationError(f"Page fetch failed ({exc.reason}) for {target}") from exc
        except OSError as exc:
            raise AutomationError(f"Network error fetching {target}: {exc}") from exc

    def inspect_url(self, url: str, *, allow_private: bool = False) -> dict[str, Any]:
        target = normalize_url(url, allow_private=allow_private)
        page = self.fetch_page(target, allow_private=allow_private)
        inspection = inspect_html(page["html"], base_url=target)
        inspection["fetch_bytes"] = page["bytes"]
        inspection["fetch_status"] = page["status"]
        return inspection

    def inspect_html_string(self, html: str, base_url: str = "") -> dict[str, Any]:
        return inspect_html(html, base_url=base_url)

    # -------- high-level autofill (open + fill + submit)
    def autofill(
        self,
        url: str,
        profile_fields: dict[str, str] | None = None,
        *,
        profile_name: str | None = None,
        field_overrides: dict[str, str] | None = None,
        submit: bool = False,
        open_browser: bool = True,
        browser: str = "default",
        headless: bool = False,
        wait_seconds: float = 1.0,
        allow_private: bool = False,
    ) -> dict[str, Any]:
        """
        Full workflow: open the site, inspect forms, map to profile, execute fill (and optionally submit).

        Returns a detailed report that is truthful about what was actually automated vs. what needs
        user review (since not every site can be driven headlessly without installed drivers).
        """
        target = normalize_url(url, allow_private=allow_private)
        if not profile_fields:
            raise AutomationError("No identity profile provided — save your details first with save_identity.")

        # Merge overrides (user explicitly provided field values for this run)
        effective: dict[str, str] = dict(profile_fields)
        if field_overrides:
            for k, v in field_overrides.items():
                if v and str(v).strip():
                    effective[k.strip().lower()] = str(v).strip()

        inspection = self.inspect_url(target, allow_private=allow_private)
        plan = build_fill_plan(inspection, effective, submit=submit)

        # Backend selection
        backend = self._detect_backend()
        execution_report: dict[str, Any] = {"backend": backend, "executed": False}

        # Always generate JS snippet as an artifact (works everywhere, user can paste)
        js_snippet = generate_js_snippet(plan)

        # Try to save snippet next to allowed roots if files manager available and url implies file path? No.
        # Instead we return it in response and also try to use native drivers if available.

        if backend == "playwright":
            execution_report = self._execute_via_playwright(target, plan, wait_seconds)
        elif backend == "selenium":
            execution_report = self._execute_via_selenium(target, plan, wait_seconds)
        else:
            # No driver: open browser for user + provide JS snippet + try keyboard assist if Windows
            if open_browser and not headless:
                try:
                    self.open_website(
                        target,
                        browser=browser,
                        headless=False,
                        allow_private=allow_private,
                    )
                    execution_report["browser_opened"] = True
                    execution_report["note"] = "Opened in system browser. Use the generated JS snippet to fill instantly."
                except AutomationError as exc:
                    execution_report["browser_opened"] = False
                    execution_report["browser_error"] = str(exc)
            # Try Windows text-typing assist for simple cases? We can't know form focus.
            execution_report["assist_snippet_available"] = True

        # Build user-facing summary
        return {
            "url": target,
            "inspection": inspection,
            "plan": {
                "total_fields": plan.total_fields,
                "mappable": plan.mappable,
                "missing_in_profile": plan.missing,
                "actions": plan.actions,
                "submit_selector": plan.submit_selector,
            },
            "execution": execution_report,
            "js_snippet": js_snippet,
            "profile_fields_used": list(effective.keys()),
        }

    def _detect_backend(self) -> str:
        # Prefer playwright because it handles Shadow DOM, SPA waits, and is more reliable.
        # find_spec checks availability without importing (importing playwright can
        # take seconds and is only needed once a driver is actually used).
        if importlib.util.find_spec("playwright") is not None:
            return "playwright"
        if importlib.util.find_spec("selenium") is not None:
            return "selenium"
        return "native"

    # --- Playwright driver ---
    def _execute_via_playwright(self, url: str, plan: FillPlan, wait_seconds: float) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            return {"backend": "playwright", "executed": False, "error": f"Playwright not installed: {exc}"}
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(int(wait_seconds * 1000))
                filled = 0
                errors: list[str] = []
                for act in plan.actions:
                    sel = act["selector"]
                    val = act["value"]
                    try:
                        loc = page.locator(sel).first
                        if loc.count() == 0 and act.get("field_meta", {}).get("name"):
                            loc = page.locator(f"[name=\"{act['field_meta']['name']}\"]").first
                        if loc.count() > 0:
                            # Handle different input types
                            tag = (act.get("field_meta", {}).get("type") or "text").lower()
                            if tag in {"checkbox", "radio"}:
                                # naive: check if value truthy
                                if str(val).lower() in {"true", "1", "yes", "on", "checked"}:
                                    loc.check()
                            elif tag == "select" or loc.evaluate("el => el.tagName") == "SELECT":
                                loc.select_option(val)
                            else:
                                loc.fill(val)
                            filled += 1
                        else:
                            errors.append(f"Not found: {sel} ({act['canonical']})")
                    except Exception as exc:
                        errors.append(f"{sel}: {exc}")
                if plan.submit_selector:
                    try:
                        page.locator(plan.submit_selector).first.click(timeout=5000)
                        # wait a bit for navigation/submission
                        page.wait_for_timeout(1500)
                    except Exception as exc:
                        errors.append(f"Submit failed ({plan.submit_selector}): {exc}")
                # Take screenshot proof if possible (optional)
                browser.close()
                return {"backend": "playwright", "executed": True, "filled": filled, "total": len(plan.actions), "errors": errors, "submitted": bool(plan.submit_selector)}
        except Exception as exc:
            LOGGER.exception("Playwright execution failed")
            return {"backend": "playwright", "executed": False, "error": str(exc)}

    # --- Selenium driver ---
    def _execute_via_selenium(self, url: str, plan: FillPlan, wait_seconds: float) -> dict[str, Any]:
        try:
            from selenium import webdriver  # type: ignore
            from selenium.webdriver.chrome.options import Options  # type: ignore
            from selenium.webdriver.common.by import By  # type: ignore
        except ImportError as exc:
            return {"backend": "selenium", "executed": False, "error": f"Selenium not installed: {exc}"}
        driver = None
        try:
            opts = Options()
            opts.add_argument("--start-maximized")
            driver = webdriver.Chrome(options=opts)
            driver.get(url)
            time.sleep(wait_seconds)
            filled = 0
            errors: list[str] = []
            for act in plan.actions:
                sel = act["selector"]
                val = act["value"]
                try:
                    el = None
                    # Translate our simple selectors to Selenium By
                    if sel.startswith("#"):
                        el = driver.find_element(By.ID, sel[1:])
                    elif sel.startswith("[name="):
                        # extract name value
                        m = re.search(r'\[name="([^"]+)"\]', sel)
                        if m:
                            el = driver.find_element(By.NAME, m.group(1))
                        else:
                            el = driver.find_element(By.CSS_SELECTOR, sel)
                    else:
                        el = driver.find_element(By.CSS_SELECTOR, sel)
                    tag = (act.get("field_meta", {}).get("type") or "text").lower()
                    if tag in {"checkbox", "radio"}:
                        if str(val).lower() in {"true", "1", "yes", "on"}:
                            if not el.is_selected():
                                el.click()
                    elif el.tag_name.lower() == "select":
                        from selenium.webdriver.support.ui import Select  # type: ignore

                        Select(el).select_by_visible_text(val) if any(o.text == val for o in Select(el).options) else Select(el).select_by_value(val)
                    else:
                        el.clear()
                        el.send_keys(val)
                    filled += 1
                except Exception as exc:
                    errors.append(f"{sel}: {exc}")
            if plan.submit_selector:
                try:
                    driver.find_element(By.CSS_SELECTOR, plan.submit_selector).click()
                    time.sleep(1.5)
                except Exception as exc:
                    errors.append(f"Submit failed: {exc}")
            return {"backend": "selenium", "executed": True, "filled": filled, "total": len(plan.actions), "errors": errors, "submitted": bool(plan.submit_selector)}
        except Exception as exc:
            return {"backend": "selenium", "executed": False, "error": str(exc)}
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    # -------- single-field helpers (for granular control)
    def fill_field(self, url: str, selector: str, value: str, *, wait_seconds: float = 1.0, allow_private: bool = False) -> dict[str, Any]:
        target = normalize_url(url, allow_private=allow_private)
        # For now, synthesize a plan with one action and run via best backend
        inspection = self.inspect_url(target, allow_private=allow_private)
        # Reuse the inspection: report whether the selector matches a detected
        # field instead of throwing the fetched document away.
        known_selectors: set[str] = set()
        for form in inspection.get("forms") or []:
            for field in form.get("fields") or []:
                known_selectors.update(str(s).strip() for s in field.get("selectors") or [])
                if field.get("id"):
                    known_selectors.add(f"#{field['id']}")
        selector_present = selector.strip() in known_selectors if known_selectors else None
        plan = FillPlan(url=target, total_fields=1, mappable=1, missing=0, actions=[{"selector": selector, "value": value, "canonical": "custom", "confidence": 10, "field_meta": {}, "action": "fill"}], submit_selector=None)
        backend = self._detect_backend()
        if backend == "playwright":
            res = self._execute_via_playwright(target, plan, wait_seconds)
        elif backend == "selenium":
            res = self._execute_via_selenium(target, plan, wait_seconds)
        else:
            # Native fallback: open browser and advise JS (don't crash if headless env has no browser)
            try:
                self.open_website(target, allow_private=allow_private)
                opened = True
            except Exception as exc:
                opened = False
                LOGGER.info("Native fill fallback could not open browser: %s", exc)
            prefix = f"Opened {target}. " if opened else ""
            # json.dumps produces a valid single-line JS string literal, so values
            # containing quotes, apostrophes, backslashes or newlines stay intact.
            js_code = (
                f"document.querySelector({json.dumps(selector)}).value="
                f"{json.dumps(value)}"
            )
            res = {"backend": "native", "executed": False, "browser_opened": opened, "js": js_code, "note": prefix + f"Paste into console: {js_code}"}
        return {
            "url": target,
            "selector": selector,
            "selector_present_on_page": selector_present,
            "value_preview": value[:40],
            "result": res,
        }

    def click_element(self, url: str, selector: str, *, wait_seconds: float = 1.0, allow_private: bool = False) -> dict[str, Any]:
        target = normalize_url(url, allow_private=allow_private)
        if not allow_private:
            # Drivers perform their own HTTP request, so validate again with DNS
            # instead of relying only on the static normaliser.
            ensure_public_fetch_url(target)
        backend = self._detect_backend()
        if backend == "playwright":
            try:
                from playwright.sync_api import sync_playwright  # type: ignore

                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=False)
                    page = browser.new_page()
                    page.goto(target, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(int(wait_seconds * 1000))
                    page.locator(selector).first.click(timeout=5000)
                    browser.close()
                    return {"url": target, "selector": selector, "clicked": True, "backend": backend}
            except Exception as exc:
                return {"url": target, "selector": selector, "clicked": False, "error": str(exc), "backend": backend}
        elif backend == "selenium":
            try:
                from selenium import webdriver  # type: ignore
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.common.by import By

                opts = Options()
                driver = webdriver.Chrome(options=opts)
                driver.get(target)
                time.sleep(wait_seconds)
                driver.find_element(By.CSS_SELECTOR, selector).click()
                time.sleep(0.5)
                driver.quit()
                return {"url": target, "selector": selector, "clicked": True, "backend": backend}
            except Exception as exc:
                return {"url": target, "selector": selector, "clicked": False, "error": str(exc), "backend": backend}
        else:
            return {"url": target, "selector": selector, "clicked": False, "backend": "native", "note": "No driver installed; install 'playwright' (pip install playwright && playwright install chromium) for one-click."}
