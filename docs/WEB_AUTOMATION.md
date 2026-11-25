# HINATA Web Automation — Open, Fill & Submit Any Website

> Your Hinata is now **strong** enough to handle any web form: it opens the site, fills your saved details, and can submit — all offline, all local, no cloud.

---

## What it does

| Capability | How it works |
|---|---|
| **Open any website** | `open website google.com` → opens in your default browser (Chrome/Edge/Firefox/Brave). Add `in chrome` or `in brave` to pick a browser. |
| **Save your details once** | `save my identity as personal: first_name=Saurav, last_name=Kushwaha, email=saurav@example.com, phone=9876543210, city=Kolkata, address_line1=123 MG Road, zip=700001` |
| **Fill any form** | `autofill https://example.com/form with personal` → detects every field, maps to your profile, fills via the best available backend. |
| **Submit** | `autofill https://example.com/form with personal and submit` → fills *and* clicks submit (review first for payment forms). |
| **Inspect** | `inspect forms on https://example.com` → shows all forms/fields + CSS selectors. |
| **One field** | `web_fill_field` / `web_click` tool APIs for granular control. |

All data stays in your local encrypted database (`identity_profiles` table). Sensitive fields (PAN, Aadhaar, card) are masked in logs.

---

## Quick start (no LLM needed)

```bash
# 1. Save your profile (do once)
hinata ask "save my identity as personal: first_name=Saurav, last_name=Kushwaha, email=saurav@example.com, phone=9876543210, city=Kolkata, address_line1=123 MG Road, zip=700001, country=India, company=AI Automation"

# 2. Check it
hinata ask "list my identities"
hinata ask "show my profile personal"

# 3. Open a site
hinata ask "open website https://google.com"

# 4. Inspect a form
hinata ask "inspect forms on https://example.com"

# 5. Autofill (open + fill)
hinata ask "autofill https://example.com/form with personal"

# 6. Autofill + submit
hinata ask "autofill https://example.com/form with personal and submit"
```

Every command works **typed** and **voice**. Voice: *"Hinata, open Google"* → *"Fill my details on example dot com"* → *"Submit the form"*.

---

## How the filling works (so strong)

### 1. Fetch & Inspect (stdlib only, no deps)
- Uses standard `urllib` + `HTMLParser` to find every `<form>`, `<input>`, `<select>`, `<textarea>`, `<button>`.
- Extracts `name`, `id`, `placeholder`, `autocomplete`, `aria-label`, `<label for=…>`, `type`.
- Generates CSS selectors for every field.

### 2. Intelligent mapping (50+ fields)
Canonical profile fields + aliases + autocomplete hints:
`first_name`, `last_name`, `full_name`, `email`, `phone`, `address_line1`, `city`, `state`, `zip`, `country`, `date_of_birth`, `company`, `job_title`, `website`, `pan`, `aadhaar`, `passport_number`, `card_holder`, `card_number`, `card_expiry`, `card_cvv`, … (see `hinata/core/identity_vault.py`).

Heuristic score per field: `autocomplete` (10) > `name` (8) > `id` (7) > `label` (6) > `placeholder` (5). Fuzzy fallback handles `userEmailAddress`, `fname`, `zipcode`, etc. Payment/ID fields auto-detected.

### 3. Multi-backend execution (best available)

| Backend | When | How strong |
|---|---|---|
| **Playwright** (`pip install playwright && playwright install chromium`) | Installed | Best: handles SPA, React/Vue, shadow DOM, waits, iframes. |
| **Selenium** (`pip install selenium` + ChromeDriver) | Fallback | Good: real browser control. |
| **Native** (always) | No drivers | Strong: opens browser + gives you a **one-paste JS snippet** that fills *any* site via DevTools Console or as a Bookmarklet. Works even without drivers! |

The JS snippet:
- Pierces shadow DOM + same-origin iframes
- Uses native `value` setter to trigger React/Vue listeners
- Dispatches `input`/`change`/`blur`
- Handles `<select>` (by value or visible text) + `checkbox`/`radio`
- Optionally clicks submit when you asked

You get the snippet in `data.js_snippet` — copy it, or run `write clipboard <snippet>`.

---

## Supported forms

- Text, email, tel, number, password, date, URL, hidden (ignored), select, textarea, checkbox, radio
- `autocomplete` hints (`given-name`, `family-name`, `email`, `tel`, `street-address`, `postal-code`, `country`, `bday`, `organization`, `cc-number`, …)
- Labels (`<label for>` and wrap-labels)
- Multi-form pages, orphan fields, synthetic forms
- SPA/dynamic forms (Playwright waits for elements; JS snippet waits via MutationObserver)

---

## Security

- **Local only**: profiles stored in `~/.local/share/hinata/hinata.db` (or `%LOCALAPPDATA%\HINATA`) — optionally envelope-encrypted when `security.encrypt_at_rest = true`.
- **No raw passwords**: `save_identity` refuses fields named `password`, `passwd`, `secret`, `token`, `api_key`.
- **Payment safety**: if your profile contains payment/ID fields and the form maps them, **auto-submit is disabled** — you must review in the browser. Remove payment from the profile for non-payment bulk autofill.
- **SSRF-safe**: `open_website` + `inspect_forms` reject `.local` mDNS by default; `fetch_page` validates URLs. Loopback (`127.0.0.1`, `localhost`) allowed for local testing.
- **Confirmation-gated**: `save_identity`, `web_autofill`, `web_fill_field`, `web_click` are `CONFIRM` tier — user must approve. `SENSITIVE` delete needs PIN.
- **Redacted logs**: sensitive values masked (`***1234`).

---

## Natural language examples

```
open website https://forms.gle/xxx
go to amazon.com
open website github.com in chrome

save my identity as personal: first_name=Saurav, last_name=Kushwaha, email=saurav@example.com, phone=9876543210
save my identity as work: first_name=Saurav, company=AI Automation, work_email=saurav@work.com
my email is saurav@example.com
my city is Kolkata

list my identities
show my profile personal
delete my profile old

inspect forms on https://example.com/login
autofill https://example.com/apply with personal
autofill https://example.com/apply with personal and submit
fill form https://example.com/contact
submit form https://example.com/contact
```

For LLMs, you can also pass structured JSON:
```json
{"type":"tool","tool":"save_identity","arguments":{"profile":"personal","fields_json":"{\"first_name\":\"Saurav\",\"email\":\"saurav@example.com\"}"}}
{"type":"tool","tool":"web_autofill","arguments":{"url":"https://example.com/form","profile":"personal","submit":true}}
{"type":"tool","tool":"open_website","arguments":{"url":"https://google.com","browser":"chrome"}}
{"type":"tool","tool":"inspect_forms","arguments":{"url":"https://example.com"}}
```

Field overrides (per-run, not saved):
```json
{"tool":"web_autofill","arguments":{"url":"https://example.com","profile":"personal","field_overrides_json":"{\"phone\":\"9999999999\"}"}}
```

---

## Upgrade to Playwright (optional, but strongest)

```bash
pip install playwright
playwright install chromium
# Optional: for Firefox/WebKit
playwright install firefox webkit
```

Now `web_autofill` will launch a real Chromium window, wait for the page, fill every mapped field, and click submit — no manual paste needed.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Private/local URL blocked` | You passed `192.168.x.x` or `.local`. For intranet, call `open_website` with `allow_private` (via tool API) or use public DNS. |
| `No identity profile found` | Run `save my identity …` first, then `list identities`. |
| `No fields matched` | Run `inspect forms on <url>` to see what Hinata sees. Some forms are rendered entirely by JS after load — install Playwright for those. |
| `Browser refused to open` | On headless Linux with no `$DISPLAY`, use `open_website` with `headless: true` or just use the JS snippet path (native still works). |
| Payment not auto-submitting | Intentional: review the filled form in the browser, then submit manually. Remove `card_*` from profile if you don't need payment autofill. |
| Need to update one field | `my phone is 9876543210` or `save my identity as personal: phone=9876543210` (upserts). |

---

## Code pointers

- Identity storage: `hinata/core/identity_vault.py` — `CANONICAL_FIELDS`, `IdentityVault`
- Automation: `hinata/automation/web_automation.py` — `FormInspector`, `heuristic_canonical_for_field`, `WebAutomation`, `generate_js_snippet`
- Validation: `hinata/security/action_validator.py`
- Intents: `hinata/core/intent_parser.py`
- Execution: `hinata/core/orchestrator.py` (`_run_tool` handlers)
- Workers: `hinata/agents/workers.py` (execution worker)
- Tool manifests: `hinata/core/tool_registry.py`

---

## Changelog

- **2026-11-25**: Initial Web Automation release — 9 new tools, identity vault, 50+ field heuristics, triple-backend (Playwright/Selenium/native+JS), docs.
