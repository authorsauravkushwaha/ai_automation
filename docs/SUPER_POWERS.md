# HINATA Super Powers — Everything Your Local Assistant Can Now Do

> One assistant, 50+ local tools, zero cloud. Hinata is now your **browser, inbox, file manager, researcher, system admin, and dev partner**.

---

## 1) 🌐 Web Automation — Open, Fill, Submit ANY Site
**You asked, we delivered — strongest in class.**

| You say | Hinata does |
|---|---|
| `open website google.com in chrome` | Opens in Chrome/Edge/Firefox/Brave (or default). |
| `save my identity as personal: first_name=Saurav, email=...` | Stores 50+ fields locally (PAN/Aadhaar/card masked). |
| `inspect forms on https://example.com` | Lists every field + CSS selectors. |
| `autofill https://example.com/apply with personal` | Fetches → maps fields (20 heuristic) → fills via Playwright/Selenium or one-paste JS bookmarklet. |
| `autofill ... with personal and submit` | Fills + clicks submit (payment forms require manual review). |

**Field coverage**: `first_name`, `last_name`, `email`, `phone`, `address`, `city`, `zip`, `company`, `job_title`, `pan`, `aadhaar`, `card_number`… with aliases (`fname`, `zipcode`, `tel`) and `autocomplete` hints.

**Backends**: Playwright > Selenium > Native+JS (shadow-DOM/iframe-aware).

---

## 2) 🕷️ Smart Scraper — Turn Any Site Into Data
```bash
scrape tables from https://example.com
scrape lists from https://news.ycombinator.com
monitor price for https://amazon.com/dp/xxx
crawl pages from https://example.com max 10 pages
scrape_export dest_path Documents/data.json format json
```
- Table parsing with headers/rows, List headings, Price + `in_stock/out_of_stock`, Pagination crawler with `link_pattern`.
- Export to `CSV`/`JSON` inside allowed roots.

---

## 3) 📁 File Wizard — Strongest Local Organizer
```bash
analyze storage for ~/Downloads
find duplicates in ~/Documents
organize folder ~/Downloads by type   # (dry_run default, add CONFIRM to execute)
batch rename ~/Downloads numbered prefix vacation_   # also: find_replace, lowercase, prefix
convert file Notes.md to Notes.html  # md↔html, json↔csv, txt cleanup
clean temp in ~/Downloads
quick cleanup ~/Downloads
```
- Categories: Images, Videos, Documents, Archives, Code… 
- Duplicate detection via size → quick-hash → full-hash.

---

## 4) 💌 Communication Center — Email, Templates, Reminders
```bash
draft email to hr@acme.com using job_application subject "SDE role"
# auto-fills {position, company, intro, highlights, full_name, email, phone} from your identity vault

list drafts
show template job_application

remind me "buy milk" in 2 hours
create reminder "team standup" at tomorrow 9am
list reminders
broadcast "Deploy done — checks passed!"  # local clipboard/file mock (SMTP opt-in)
```
Templates: `job_application`, `follow_up`, `meeting_request`, `thank_you`, `cold_outreach`.  
Time parsing: `in 2 hours`, `tomorrow 3pm`, `2026-12-01 10:00`. Stored in `reminders` table.

---

## 5) 🖥️ System Tuner & Guardian
```bash
health check          # disk >90%, memory, CPU snapshot
network diag          # DNS + 8.8.8.8/1.1.1.1 connectivity
backup ~/Documents to ~/Backups
quick cleanup ~/Downloads dry_run false
search processes for chrome
```
- Prunes old backups (`max_age_days`), bounded 500 files/200 MB, safe temp patterns.

---

## 6) 🔎 Research Synthesizer — Deep Multi-Query Research
```bash
deep research on "quantum computing 2026"
compare quantum computing versus classical computing
summarize url https://example.com/article
```
- Fires 4 parallel queries (`topic`, `overview`, `latest news`, `explained`), deduplicates, synthesizes with LLM or fallback excerpts, cites `[1] Title — URL`.

---

## 7) 👩‍💻 Dev Autopilot — Git, Review, Dependencies, Tests
```bash
git status
git diff staged
git commit "feat: add scraper" in ~/myproject
code review ~/myproject
dependency audit ~/myproject
run tests ~/myproject
dev_run_tests
```
- Scans `.py` for long lines, bare `except:`, hardcoded secrets, missing docstrings. Manifests: `requirements.txt`, `pyproject.toml`, `package.json`.

---

## 8) 🧠 Identity & Memory — Already Yours
- `My name is Saurav` → syncs to identity vault automatically for future fills.
- `my email is saurav@example.com`, `my city is Kolkata`
- `list my identities` / `show my profile personal` / `delete my profile old`

---

## 9) 🎛️ How to Try (No LLM needed)
```bash
hinata ask "health check"
hinata ask "analyze storage for ."

# Draft an email (identity auto-filled)
hinata ask "draft email to boss@co.com using meeting_request subject 'Q1 review'"

# File wizard (safe dry-run)
hinata ask "organize folder Downloads by type"

# Research
hinata ask "deep research on AI automation"

# Scrape
hinata ask "scrape tables from https://en.wikipedia.org/wiki/List_of_countries_by_population"
```

All 50+ tools are **permission-gated** (`SAFE` instant, `CONFIRM` needs yes, `SENSITIVE` needs PIN). Red Alert Lockdown still freezes mutable tools.

---

## 10) Code Map
- `automation/smart_scraper.py` — `SmartScraper`
- `automation/file_wizard.py` — `FileWizard`
- `automation/communication_center.py` — `CommunicationCenter`
- `automation/system_tuner.py` — `SystemTuner`
- `automation/research_synthesizer.py` — `ResearchSynthesizer`
- `automation/dev_assistant.py` — `DevAssistant`
- `core/identity_vault.py` + `automation/web_automation.py` — web fill power

Add Playwright for strongest browser control: `pip install playwright && playwright install chromium`
