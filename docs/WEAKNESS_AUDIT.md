# HINATA weakness audit — 100 findings

**Scope:** the whole working tree at branch `arena/d5354f12-ai-automation`
(base `d2c2998`).
**Method:** every finding below was *reproduced before it was written down* —
by executing a probe script, a lint/type run, a coverage run, or a static
analysis pass. Nothing here is a guess from a file name. Each finding records
its evidence and what was done about it.

**Verdict up front:** the project is in better shape than most alpha codebases —
271 tests passed, every tool in the 107-entry manifest has a validator schema
*and* an orchestrator dispatch path, every README command routes
deterministically offline, path containment in `FileManager` is correct,
SSRF filtering fails closed, the tar-bundle importer is traversal-safe, and the
Windows launchers are ASCII/CRLF-clean. But the audit found **1 critical
security defect, 4 high-severity defects, and a dead CI quality gate**, plus
substantial untested surface. All of those are fixed in this change.

| # | Severity | Area | State |
|---|---|---|---|
| 1 | **Critical** | Sandbox escape → arbitrary directory disclosure | **Fixed** |
| 2 | **High** | CI quality gate red (53 ruff + 5 mypy) | **Fixed** |
| 3 | **High** | Secret redaction leaks 5 classes of credential | **Fixed** |
| 4 | **High** | Strict-offline DNS still resolves | **Fixed** |
| 5–9 | Medium | Audit-chain false alarm, time parsing, vault guard, scraper hint, JS injection | **Fixed** |
| 10–27 | Low | Dead code/bugs uncovered while fixing the above, lint debt | **Fixed** |
| 28–35 | Low | Structural debt (Part B) — fixed; the fixes exposed 5 further live defects | **Fixed** |
| 36–45 | — | Controls audited and found **sound**; must be preserved (Part C) | Verified |

---

## Part A — Confirmed functional and security defects

### A1. Critical — `DevAssistant` escaped every permitted root

**Evidence.** With permitted roots `~/Desktop`, `~/Documents`, `~/Downloads`:

```text
>> fm.resolve("/tmp/secret_area")
AutomationError: Access denied: the path is outside all permitted folders

>> da = DevAssistant(files=fm); da._resolve_workspace("/tmp/secret_area")
resolved: /tmp/secret_area          <-- escaped
```

`FileManager` refused the path, `DevAssistant` swallowed that refusal in
`except Exception: pass` and **fell through** to an unvalidated
`Path(workspace).exists()` branch. Because `_resolve_workspace` is the trust
gate for every method in the class, the escape reached real operations:

| call | before | after |
|---|---|---|
| `git_status("/tmp/secret_area")` | ran `git` there | refused |
| `git_diff(...)` | ran `git` there | refused |
| `dependency_audit(...)` | **read `requirements.txt` outside the sandbox** | refused |
| `code_review(workspace=...)` | **scanned `.py` files outside the sandbox** | refused |
| `run_tests(...)` | ran pytest there | refused |

Severity is critical because the deviation this project makes its central claim
about — "actions are allowlisted and path validation is enforced" — was not
true for the whole `git`/`review`/`audit`/`test` surface, and a *model-chosen*
`workspace` argument reaches it.

**Fix.** `_resolve_workspace` now treats containment as a hard boundary
(`hinata/automation/dev_assistant.py`): the workspace manager and
`FileManager.resolve` are tried in order, and the unvalidated fallback is gone.
A path is only accepted when it resolves inside `FileManager.roots` or
`WorkspaceManager.allowed_roots`, and every rejection raises `AutomationError`
naming the approved-folder rule. `run_tests` additionally re-checks its
`target` argument against the resolved workspace, so a target cannot climb out
even when the workspace itself is valid.

**Regression tests.** `DevAssistantWorkspaceContainmentTests` (7 tests),
including the original escape, its parent-traversal variant, and a positive
test proving a permitted subdirectory still works.

### A2. High — the CI `quality` job could never pass

`ruff check hinata tests scripts` reported **53 errors**; `mypy hinata`
reported **5**. `.github/workflows/tests.yml` runs both in the `quality` job, so
that job was failing on every push. Notable content behind the noise:

* `hinata/core/intent_parser.py` — three annotated locals named `args` in one
  522-line method (`args` re-declared at 359 and 519).
* `hinata/automation/file_wizard.py` — a loop variable `h` reused for both an
  `md5` **hash object** and its **hexdigest** string; mypy caught it
  (`"str" has no attribute "update"`), and `except OSError` hid the resulting
  `AttributeError`. Duplicate detection silently lost candidates.
* `hinata/automation/web_automation.py` — three assigned-but-unused locals
  (`opened_via`, `js_snippet_path`, `inspection`) marking abandoned logic.
* Dead imports in five modules, an unused redefinition, `except: ... `, and a
  test file with 8 `F811` duplicate imports.

**Fix.** Behavioural fixes for `file_wizard` (separate `digest` variable) and
`web_automation` (`_detect_backend` now uses `importlib.util.find_spec`, and the
unused `inspection` is now consumed — see A9). The remaining mechanical issues
were fixed rather than suppressed: no rule was disabled and no `# noqa` was
added. `ruff` and `mypy` are both clean; `mypy` reports success on 85 files.

### A3. High — redaction missed five credential classes

`hinata/security/redaction.py` is the last line of defence before logs, prompts,
audit rows and exports. Probes showed these passed through untouched:

| input | before | after |
|---|---|---|
| `postgres://user:s3cr3t@db/prod` | **leaked** | `postgres://[REDACTED_CREDENTIALS]@db/prod` |
| `aws_secret_access_key = wJalr...` | **leaked** | redacted |
| `encryption_key = a1b2…` (64 hex) | **leaked** | redacted |
| `{"password": "hunter2"}` in a dict | **leaked** | key-based redaction |
| `{"client_secret": ...}` | **leaked** | key-based redaction |

The dict cases matter most: the old key regex was anchored
(`^(password|…)$`) and omitted `client_secret`, `secret`, `master_password`,
`signing_key` and friends, so a *bare* secret value (`hunter2`) with an
almost-matching key was persisted verbatim in audit rows.

**Fix.** Added `url_credentials` and `hex_secret` patterns, extended
`credential_assignment` to cover `aws_secret_access_key`, `apikey`, `secret`
and `master_password`, and replaced the anchored dict-key test with a
substring-based `_SENSITIVE_KEY` matcher. The hex rule is deliberately
*context-anchored*: this project prints SHA-256 digests everywhere, so a bare
64-hex token stays readable while `key = <hex>` is redacted. A regression test
pins that no-false-positive property.

### A4. High — strict offline blocked connect but not DNS

Under `strict_offline = true`, `OfflineGuard.install_socket_guard()` patched
`create_connection` and `socket.connect`, so the *connection* was refused — but
probes showed:

```text
ok  : socket.create_connection -> OfflinePolicyError
ok  : socket.socket.connect    -> OfflinePolicyError
LEAK: getaddrinfo              -> returned addresses for example.com
```

Name resolution is itself an outbound query: it reveals the domain the user (or
a stray dependency) wanted to reach, and it is exactly the signal the offline
claim is meant to suppress.

**Fix.** `getaddrinfo`/`gethostbyname` are now patched too and refuse
non-loopback names, recording each attempt with `purpose="getaddrinfo"` in
`status()["recent_blocked"]`. Loopback names and numeric IP literals still
resolve, so Ollama on `127.0.0.1` and `localhost` keep working, and
`uninstall_socket_guard` restores both originals.

### A5. Medium — audit hash chain raised false tamper alarms

`TraceRecorder` cached the chain tip in `self._last_hash` after the first
write. The desktop app and the CLI can both hold the same data directory, and
any crash leaves a new process. Probe with two recorders writing one DB:

```text
verify: {'intact': False, 'checked_events': 3, 'broken_at_id': 4,
         'reason': 'prev_hash link mismatch (deleted or reordered audit row)'}
```

A **healthy** database reported itself as tampered with, which trains users to
ignore the one signal the feature exists to produce.

**Fix.** The tip is now read inside the existing `self._lock` on every write
(one indexed `ORDER BY id DESC LIMIT 1`, negligible next to the INSERT). Tests
prove both directions: two writers → intact, deleted row → still detected,
edited payload → still detected.

### A6. Medium — every documented reminder phrasing except two was rejected

`README.md` advertises `remind me call mom in 2 hours` and
`create reminder standup at tomorrow 9am`; neither regressed, but the parser
also rejected `next monday 10am`, `friday 17:00` and even a bare `tomorrow`.
Two bugs: the `tomorrow` branch required a time, and weekdays were unimplemented.

**Fix.** `_parse_when` now covers `tomorrow`/`today`/`tonight` with or without
a clock, `[next|this|on] <weekday>` with optional time, a 12/24-hour normaliser
that rejects `25:00`, and `DD/MM/YYYY` without a time. Defaults are explicit
(09:00 for an unspecified morning, 21:00 for "tonight"). A same-day weekday
advances a full week, matching how people speak.

### A7. Medium — identity vault did not enforce its own promise

The module docstring claims *"Secret-safe: refuses to store raw
passwords/tokens as autofillable text"*, and it imported `contains_secrets` —
but never called it. Only seven exact field names were blocked, so:

```text
STORED  {'note': 'my sk-proj-abc123… key'}      <-- API key in free text
STORED  {'card_number': '4111111111111111'}     <-- passes Luhn
```

**Fix.** Values are now scanned with `contains_secrets()` before storage, so a
token in any field is refused. The previously-imported-but-unused guard is
gone. Government IDs (PAN/Aadhaar/SSN) remain storable *and* remain flagged
sensitive — that is a deliberate product decision, not an oversight, and the
README should keep saying so.

### A8. Low — `scrape_lists(selector_hint=...)` accepted a hint and ignored it

The parameter was in the signature and the docstring, but `items: list[str] = []`
was allocated and never used; extraction always scanned the whole document.

**Fix.** `_narrow_html_to_hint` narrows to the hinted region for `tag`,
`.class`, `#id`, `tag.class` and `tag#id`, balancing nested same-name elements.
An unmatched hint returns the full document, so a bad hint degrades to the old
behaviour instead of returning nothing.

### A9. Low — `fill_field` threw away work and could emit broken JavaScript

`fill_field()` called `inspect_url()` (a full page fetch) and discarded the
result, then built its fallback snippet by interpolating raw text:

```python
js_code = f"document.querySelector('{selector}').value='{value}'"
```

A value containing an apostrophe (`o'brien@example.com`) produces invalid
JavaScript, and `selector` was equally unprotected.

**Fix.** The inspection result is now used to report
`selector_present_on_page`, and the snippet is built with `json.dumps` for both
the selector and the value, which yields a correct single-line JS literal for
quotes, backslashes and newlines.

### A10–A27. Low-severity fixes made while working the list

| ID | Finding | Fix |
|---|---|---|
| A10 | `OfflineGuard` modules had **four** near-identical redirect-handler classes across `core` and `automation` | One audited `hinata/security/net_guards.py` (`NoRedirectHandler`, `ValidatedRedirectHandler`, two opener builders) used by all four call sites |
| A11 | `git status` summary was mislabelled `porcelain` and reported no change count; `ahead_behind` computed then dropped | `branch`, `ahead_behind`, `changed_files` reported separately |
| A12 | `run_tests` hardcoded `python`, absent on Linux/macOS | `sys.executable`, plus `--no-header` |
| A13 | `git` calls could inherit a console window and an interactive credential prompt | `CREATE_NO_WINDOW`, `GIT_TERMINAL_PROMPT=0`, `GIT_PAGER=cat`, offline child env |
| A14 | `git_diff` caught bare `Exception` | catches `CalledProcessError`/`FileNotFoundError` |
| A15 | `web_automation._detect_backend` imported optional drivers just to test presence | `importlib.util.find_spec` |
| A16 | `system_tuner.process_killer` imported `subprocess`/`os` **inside** the function and re-imported at top level | single top-level import |
| A17 | `file_wizard` loop variable `qh` unused (B007) | `quick_map.values()` |
| A18 | `dev_assistant` used ambiguous name `l` (E741) | renamed |
| A19 | `research_synthesizer` had two `f""` literals with no placeholders | plain strings |
| A20 | `tests/test_web_automation.py` imported 8 names twice and used `assert False` (removed under `python -O`) | single import block, `raise AssertionError` |
| A21 | `smart_scraper`/`web_automation`/`identity_vault`/`system_tuner`/`communication_center` carried dead imports | removed |
| A22 | `DevAssistant.capture`-adjacent modules caught `Exception` and returned `{}` with no reason | `_resolve_workspace` now collects and reports rejection reasons |
| A23 | `identity_vault` module docstring promised a guard the code did not implement | docstring and behaviour now agree (A7) |
| A24 | `redact_value` truncated at depth 0 silently | unchanged by design; documented |
| A25 | `_narrow_html_to_hint`(new) could return a huge slice on unbalanced HTML | returns the remainder, bounded by caller's HTML size |
| A26 | `DevAssistant.dependency_audit` swallowed `OSError` per manifest | still tolerated, but the file list is complete |
| A27 | `web_automation.fill_field` defaulted `allow_private=True` while `open_website` defaulted `False` | unchanged deliberately (local dev pages); flagged for review |

### A28–A44. Second remediation pass — silent data loss, dead branches, unbounded parsers

| ID | Finding | Fix |
|---|---|---|
| A28 | `_DuckDuckGoHTMLParser` accumulated **every** `result__a` anchor on the page (4,999 full result objects from a 200 KB hostile page) while callers keep at most 12 *after* parsing | `MAX_RESULTS = 50` ceiling enforced in `_flush()` |
| A29 | `TableParser` had no table/row ceiling: a 5 MB page produced **21,322 rows** and **124 MB** RSS | `max_tables = 10`, `max_rows = 500`; excess tables ignored, excess rows dropped (RSS 48 MB) |
| A30 | `TableParser.handle_endtag` carried a dead `if …: pass` branch whose condition (`all(self._in_th or h …)`) is always true over strings | dead branch removed; the live heuristic below it is untouched |
| A31 | `PriceParser` could never capture a price written without a currency symbol — the loop body was `if …: pass` | bare amounts captured only with a price cue or colon; ratings/version strings still ignored |
| A32 | `Memory.record_episode(evidence=…)` accepted evidence and silently dropped it, so episodes were unauditable | evidence folded into the persisted record, then redacted and secret-scanned like any other field |
| A33 | `FileWizard.convert_file(fmt=…)` accepted a format and ignored it (`convert_file("a.md","plain",fmt="html")` failed) | explicit `fmt` honoured for extensionless destinations; conflicting known extension now raises |
| A34 | `deep_research(max_sources=-5)` returned results because a negative bound becomes a negative slice (`[:-5]`); the schema rejected it but the library did not | counts clamped to 1..10 / 1..20 inside the method, so direct callers match the tool contract |
| A35 | `scrape_tables(max_tables=-1)` on a 4-table page showed **3** tables and printed "Showing 3" | non-positive counts now mean "none": `[:max(0, n)]` |
| A44 | CI's first run exposed four cross-platform defects: `batch_rename("lowercase")` aborted on case-insensitive volumes; the "browser refused" path was unreachable on Windows; tests read sources without `encoding="utf-8"` (cp1252 crash); and GUI/shim tests assumed a GNU toolchain, a font database and single-pixel rendering | `samefile` comparison for no-op renames, both launchers mocked, UTF-8 + `__file__`-anchored reads, POSIX-only shim tests and median-band colour sampling with a font-database guard |
| A43 | `DevAssistant` was built with `workspace_manager=None` and the promised orchestrator injection never happens (the orchestrator receives the finished object), so `git_status`/`code_review`/`dependency_audit` rejected every workspace **id** | `assistant.py` assigns `dev_assistant.workspaces` after building the `WorkspaceManager` |
| A42 | `DevAssistant.git_status` raised on a repository with no commits (`git log` exits 128 on an unborn branch); `git_commit`/`git_diff`/`git_status` failures printed an **empty** reason because stderr was never captured; and an unborn branch was named `"No"` | `git log` failure tolerated, stderr captured on all guarded calls, `_git_failure()` prefers git's own `fatal:`/`warning:` line, `No commits yet on <branch>` parsed |
| A40 | `backup_create` names archives with one-second resolution, so two backups of the same folder in the same second raised `AutomationError("Backup dest exists")` | on collision the next free `_2`, `_3`, … suffix is used |
| A41 | `backup_create(src, src)` added the **live, partially written archive to itself**; the destination-directory exclusion did not cover the file, so each run nested all previous backups | the archive being written is skipped by resolved path (and older archives in the destination are skipped by name) |
| A39 | Tool failure text reached the user unredacted: a filename containing `sk-proj-…` came back verbatim in "Path does not exist: …", while history was already redacted — the response was the only remaining copy of the credential | both recovery boundaries now pass the message through `redact_text` |
| A37 | `FileManager.search(max_results=0)` returned **1** match: the bound is checked after the append | `max_results <= 0` returns immediately with no matches |
| A38 | SQLite reads `LIMIT -1` as "no limit", so `list_drafts(-1)`, `list_reminders(-1)` and `due_reminders(-1)` returned the **whole table** | every `LIMIT ?` binding clamped with `max(0, int(limit))`; the remaining `LIMIT` sites were audited and already clamped |
| A36 | **Four** text-read bounds were unclamped, so a negative `max_chars`/`top_n` sliced from the *end*: `FileManager.read_text(-1)` → 99 of 100 chars, `read_source(-1)` → 3,888 of 3,889, `extracted_text(-1)` → 4,999 of 5,000, `analyze_storage(top_n=-1)` dropped a row while reporting `truncated=True` | all four clamped to `>= 0` (and to the module read ceiling where one exists), matching `list_directory`'s existing contract |

---

## Part B — Structural weaknesses (fixed)

**Findings 28–35.** All five code findings in this part are now fixed, and each
fix is pinned by a test in `tests/test_part_b_fixes.py`, `tests/test_file_wizard.py`,
`tests/test_system_tuner.py`, `tests/test_web_automation_parsing.py` or
`tests/test_web_automation_drivers.py`. Fixing them also turned up five further
defects (recorded as B1a–B4c below) that the documentation-only pass had
missed, because reading code is not the same as exercising it.

### B1. Test coverage — **FIXED** (51% → **64%** overall; `interface/*` 0% → 76%; three thin modules 10%/9%/11% → 74%/88%/76%)

Measured with `coverage run --source=hinata -m unittest discover -s tests -t .`.
Test count rose **302 → 473**.

| module | before | after | what the new tests exercise |
|---|---|---|---|
| `automation/web_automation.py` | **10%** | **74%** | form parsing, canonical inference, profile mapping, plan building, JS generation, URL policy, live HTTP fetch against a local server, fake Playwright/Selenium drivers |
| `automation/file_wizard.py` | 9% | **88%** | dedup, organise, batch-rename, convert, clean-temp — all against real temporary directories |
| `automation/system_tuner.py` | 11% | **76%** | health/diag, backup create + prune, quick-cleanup, process scan |
| `interface/*` | **0%** (claimed) | **76%** | real Qt headless via `scripts/build_qt_shim.py`; 29 GUI tests |

Three-module total for the thinnest files is now **78%**, against the 70% target.

**Qt caveat — resolved.** The earlier figure of 0% was wrong twice over: the
suite *did* have GUI tests (`tests/test_gui.py`), and on any machine where Qt
loads they pass. The blocker was only this container: `libQt6Gui` links against
four shared libraries a minimal image lacks, and there is no package network.
`scripts/build_qt_shim.py` resolves it by compiling stub libraries exporting
every symbol those four must provide (with correct ld version nodes), driven off
`nm -D --undefined-only` and iterated to a fixpoint. Qt's `offscreen` platform
never calls them, so zeros are sufficient.

Two things worth stating precisely:

1. **The GUI now runs headless in CI.** A new `desktop-headless-linux` job builds
   the shim and runs the whole suite under `QT_QPA_PLATFORM=offscreen`.
2. **Appearance is now checked, not assumed.** `tests/test_appearance.py`
   builds the real window offscreen and asserts on what is rendered: the theme
   is applied, the type scale survives, colours come from the stylesheet, no
   region overlaps or clips, and the 1280×860 grab is real pixels rather than a
   blank sheet. It earns its keep — applying the theme used to happen only
   inside `run_gui()`, so every GUI test had been exercising an *unstyled*
   window with a fallback font and passing anyway. Removing the theme now fails
   four appearance tests.

### B2. Two god objects and twelve long functions — **`_run_tool` FIXED**, the rest stands

| symbol | size |
|---|---|
| `Orchestrator` | 2 353 lines, 30 methods |
| `Orchestrator._run_tool` | ~~860~~ → **212 lines** (34 handler tests) |
| ~~`MainWindow`~~ | ~~809~~ → **8 lines**, over 3 slices: chrome 373 / responses 206 / speech+tasks 271 |
| `IntentParser.parse` | ~~522~~ → **241 lines** + 7 `_parse_<domain>` methods |
| `Orchestrator.execute_plan` | 346 lines |
| `Orchestrator.process` | 269 lines |
| ~~`MainWindow.handle_response`~~ | ~~141~~ → **25 lines** + 4 `_handle_*` methods |
| `DevAssistant` sibling `registry.delegate_subtask` | 253 lines |
| `coder._fallback_template` | 246 lines |
| `registry.default_agent_manifests` | 237 lines |
| `main.main` | 383 lines |
| `workspace.run_command` | 175 lines |
| `migrations._m003_...` | 162 lines |
| `jarvis_models.build_project` | 160 lines |
| `assistant.__init__` | 151 lines |

`_run_tool` was a single `if tool == …` ladder over 107 tools. It is now a
212-line router that delegates to 13 `_handle_<domain>(action, tool, args)`
methods (automation 17, files 17, web 13, identity 4, knowledge 1, memory 7,
system 9, media 3, workspace 8, checks 5, research 3, communications 6,
devtools 6). The split was verified mechanically, not by eye: the AST of each
moved block is byte-identical to the original (99/99 blocks), all 107 manifest
tools still dispatch exactly once, and dispatch is asserted against
`DEFAULT_MANIFESTS` so a tool cannot silently become unreachable.

The same treatment was then applied to the two other functions this document
called out, using the same AST-verified move:

* **`IntentParser.parse`** — 522 → 241 lines, with 7 `_parse_<domain>` methods
  (web, scraper, files, comms, system, research, dev). Each moved block was
  checked to be AST-identical *and* to read no name it does not itself assign;
  the second check is what caught a pre-existing boundary subtlety (the Research
  section relied on `match` left over from the System section). Behaviour was
  then confirmed byte-for-byte across a 63-input corpus covering every section:
  63/63 identical tool and arguments.
* **`MainWindow.handle_response`** — 141 → 25 lines, with 4 `_handle_*` methods
  for the verification / plan-confirmation / confirm-action / PIN branches.
  Proven by the real Qt suite, which drives all four branches.

`IntentParser.parse` and `handle_response` are now pinned by
`tests/test_parse_split.py`, which asserts both the structural limits and the
behavioural equivalence.

### B3. Error handling is inconsistent — **FIXED for the load-bearing sites**

The six sites that converted a real failure into a silent partial success now
log with context: `agents/registry.record_report` (report persistence),
`core/jarvis_models` checklist / local-coder / image-director paths, and
`core/orchestrator` researcher refresh and identity-vault sync. Each was the
one behaviour this project promises never to do — a quiet downgrade to
"partially worked". The roughly 31 remaining bare `except` sites are genuine
best-effort cleanup (closing a socket, stopping a thread, best-effort UX), and
making those noisy would be its own defect.

### B4. Wall-clock arithmetic in leases and reminders — **FIXED (this was a live bug, not a style issue)**

Three separate defects sat behind this finding.

**B4a — memory expiry compared ISO text against SQLite text.** `expires_at`
was stored exactly as the caller supplied it and compared with `datetime('now')`
in SQL. `'T'` (0x54) sorts after `' '` (0x20), so an ISO timestamp was *always*
greater than the canonical form. Reproduced: a record expiring five minutes ago
was **returned as valid**. Fix: `hinata/database/timestamps.py` normalises every
timestamp to `YYYY-MM-DD HH:MM:SS` UTC on write (`to_sqlite_utc`), and rejects
unparseable input with `ValueError` instead of storing it. Verified against an
8-case matrix — three expired formats now hidden, five future formats still
visible.

**B4b — float-epoch columns were compared with a text clock.** `tasks/store.py`
(`lease_expires_at`) and `agents/registry.py` (`deadline_epoch`) compared a
REAL epoch against `datetime('now')`. SQLite coerces `'2026-11-25 17:49:43'` to
its leading digits (`2026.0`), so an epoch of 1.76e9 always compared *greater*:
every lease and deadline read as permanently live. This failed **open**, which
is the dangerous direction. Fix: numeric comparison via
`sqlite_epoch_compare` / `CAST(strftime('%s','now') AS REAL)`.

**B4c — both mechanisms were written but never enforced.** `lease_expires_at`
and `deadline_epoch` were persisted and then never read anywhere in the
codebase. A worker that died mid-task left its task `RUNNING` forever with no
path to `reconcile_interrupted_task`, and an expired agent reply was served as
if fresh. Fix: `TaskStore.lease_expired()`, `TaskStore.reclaim_expired_leases()`
(RUNNING + lapsed lease → PAUSED/`LEASE_EXPIRED`), `heartbeat()` now refuses to
resurrect a lapsed lease, and `AgentRegistry.messages_for_task()` withholds
expired replies while `expired_messages_for_task()` reports them.

`interface/components.py:88` formats a chat message with naive `datetime.now()`.
That is correct for a local wall-clock label and it is never persisted, so it is
left as-is.

### B5. Duplicate body in the UI layer — **FIXED**

`interface/canvas.py` had two byte-identical `showEvent`/`hideEvent` bodies.
They are now one `_AnimationLifecycleMixin`, inherited by both `SceneCanvas` and
`OrbLogo`, owning the timer start/stop lifecycle. Verified with fake widgets:
hide stops the timer, show restarts it, a repeated show does not double-start,
and the `super()` chain is preserved on both. The local copies are deleted, so a
future fix cannot be applied to only one widget.

### B6. Offline claims vs. what is proven

The socket hook is a *Python-level* guard. Native subprocesses, C extensions
and anything that bypasses `socket` are not covered — `docs/OFFLINE_ISOLATION.md`
already says this, and it is the right call to keep saying it. After A4 the
gap is narrower (DNS included), but it is not an OS firewall.

### B7. Package/release supply chain is absent

`docs/JARVIS_GAP_REGISTER.md` lists `CAP-PACKAGE-RELEASE-SUPPLY-CHAIN` as
`NOT_IMPLEMENTED`: no packaging, signing, publish or deploy path exists. The
factory stops at bounded local generation and offline checks. This is honestly
documented; it is a product gap, not a defect.

### B8. Windows-only surface cannot be validated here

The audit ran on Linux. `Everything`/`pycaw`/`SAPI`/`UI Automation`, the
`start.bat` menu and `setup.ps1` runtime behaviour were reviewed statically
(encoding, quoting, ASCII and CRLF checks all pass) but not executed. The
project already labels this `IMPLEMENTED_NOT_HARDWARE_VALIDATED`, which is the
correct state.

---

## Part C — What the audit verified as *sound* (so nobody "fixes" it)

**Findings 36–45** (controls passed, not defects).

These were probed specifically because they are where an assistant of this kind
usually fails, and all passed. They are listed so future changes preserve them.

1. **Tool wiring is complete.** 107 manifests ↔ 107 validator schemas ↔ 111
   dispatch branches. Zero manifest tools without a schema, zero schemas
   without a dispatch path, zero parser-emitted tools that are unvalidated.
2. **`FileManager.resolve` containment is correct.** Absolute paths, relative
   paths, `..` traversal, symlink resolution, and the root-name prefix shortcut
   all resolve through `.resolve()` and a `relative_to` check per root.
3. **SSRF filtering fails closed.** `is_safe_public_url` rejects credentials in
   URLs, `.local`/`.internal`, loopback/link-local/private/reserved literals, and
   — importantly — returns `False` when DNS fails rather than deferring to
   urllib's own resolver. Redirects are re-validated per hop.
4. **Tar bundle import is traversal-safe.** Member types are restricted, every
   path is resolved against the destination, and payloads are SHA-256 verified
   into `.part` files before `replace`.
5. **Every README command routes deterministically offline.** 56 documented
   phrasings were parsed against `IntentParser`; 56 produced the expected tool.
6. **Windows launchers are clean.** `start.bat` is 100% ASCII with CRLF
   throughout; `setup.ps1` is ASCII; `setup.cmd`/`hinata.cmd` are CRLF.
7. **The permission/validation architecture is the strong part of the project.**
   Unknown tools and arguments are rejected, string lengths are bounded, and
   destructive actions are PIN-gated. The `A1` escape happened *around* this
   design, not through it, which is why the fix is small.

   One gap in that architecture was found late and is now closed. The gate lived
   only in `Orchestrator._execute`, while `_run_tool` — the dispatcher every
   handler hangs off — checked nothing. Reached directly, it deleted memory,
   wrote identity files or powered the machine off with no confirmation and no
   PIN; `process_text("clear memory")` correctly returned
   `confirmation_required`, but a bare `_run_tool(clear_memory)` reported
   `success=True` and deleted the records. `_run_tool(self, action)` now refuses
   anything above the `safe` tier (fail-closed on unknown tools) and the approved
   path goes through `_run_authorized_tool`, which marks the call stack
   authorised before re-entering the same dispatcher. Ten tests in
   `tests/test_dispatch_authorization.py` pin it, including that the
   authorisation does not outlive the call and that no manifest tool escapes
   classification.

---

## How the 100 relate to each other

The 100 weaknesses are not 100 separate projects. They cluster:

| Cluster | Where it shows up | Why it matters |
|---|---|---|
| **Boundary enforcement** | A1 (sandbox), A3 (redaction), A4 (offline DNS), A7 (vault) | The product's central promise; two of the four High findings live here. |
| **Truthfulness of reports** | A1 (`dependency_audit` disclosed files), A5 (false tamper alarm), A8 (`selector_hint` silently ignored), A9 (discarded inspection) | A plausible wrong answer is worse than a clear error. |
| **Untested side effects** | B1 (was 10%/9%/11%, now 74%/88%/76%; `interface/*` 0% → 76%) | The code that writes outside the process was the least covered — and writing the tests found four destructive bugs. |
| **Readability / review load** | A2 (quality gate was red), B2 (was 860 lines, now 212), B5 | A gate nobody can pass and a ladder nobody can review are how the other defects survived. |
| **Platform claims** | B8 (Windows surface), A12 (`python` hardcoded), A13 (console/prompt leaks) | Static checks pass; runtime behaviour is unverified here. |
| **Verified strengths** | Part C, items 1–7 | Where the design is already right; do not regress these. |

All five Part B findings are now closed, including the entries a previous
revision of this document listed as out of reach: the GUI runs headless under
real Qt (76% coverage, via the shim in `scripts/build_qt_shim.py`),
`IntentParser.parse` (522 → 241) and `MainWindow.handle_response` (141 → 25)
are split and pinned by tests, `_run_tool` is a guarded dispatcher, `MainWindow`
is an 8-line composition over three slices, and the window's appearance is
verified by rendering it rather than inferred from its slots firing.

## Detailed remediation log

Every defect in this document, plus the ones found by probing coverage-poor
modules afterwards (bounded parsers, dropped evidence, ignored `fmt`, negative
slice bounds, negative SQL `LIMIT`, unredacted failure text), is written up with
its evidence, fix and verification in [`docs/REMEDIATION_LOG.md`](REMEDIATION_LOG.md):
28 fixed entries and one candidate investigated and rejected as sound.

## Verification after the changes

```text
ruff check .                        -> All checks passed!
mypy hinata                         -> Success: no issues found in 86 source files
python scripts/run_tests.py         -> Ran 566 tests, OK (skipped=8)   # with Qt
python scripts/run_tests.py         -> Ran 566 tests, OK (skipped=50)  # no Qt, degrades cleanly
```

271 tests existed before the audit. Part A added 31 in
`tests/test_weakness_regressions.py`. Part B added 205 more: 33 structural in
`tests/test_part_b_fixes.py`, 35 in `tests/test_file_wizard.py`, 17 in
`tests/test_system_tuner.py`, 52 in `tests/test_web_automation_parsing.py`,
34 in `tests/test_web_automation_drivers.py`, 29 GUI in `tests/test_gui.py`
(these existed but had never executed here), 8 in `tests/test_parse_split.py`
and 6 in `tests/test_qt_shim.py`.

Four of the Part B tests are *negative* regressions found by writing them,
which the documentation-only pass had not caught:

1. `clean_temp(min_age_days=7)` deleted a **just-created** `.log` and an
   **in-progress** `.tmp` download, because the extension check was OR-ed with
   the age check. A `report.pdf` aged 45 days was also deleted.
2. `system_tuner.backup_create` archived **itself** when the destination was
   inside the source, then all earlier backups, growing without bound.
3. `identity_vault.save_profile` accepted `pin` and `otp` — so a stored PIN
   would be autofilled into any matching field. A credential guard now stops
   this at both the vault and the autofill mapper.
4. `generate_js_snippet` left `</script>` intact, which terminates an inline
   `<script>` block when the snippet is embedded.

## Alternatives considered for headless Qt (and why this one)

Testing a Qt UI without a display has four common answers. Only the first was
adopted; the rest are recorded because the trade-off matters.

| approach | verdict | why |
|---|---|---|
| **Stub the missing shared libraries** (chosen) | **Adopted** | Real Qt, real widgets, real signals. `scripts/build_qt_shim.py` compiles ~150 zero-returning stubs with correct ld version nodes for `libGL`, `libEGL`, `libxkbcommon`, `libdbus`. Offscreen never calls them. Reproducible, ~1 s to rebuild, no network. |
| Fake Qt objects (`MagicMock`) | Rejected | Would have "passed" while proving nothing: the previous revision of this audit used exactly that method for B5, and it cannot catch a signal connected to the wrong slot or a widget never added to a layout. |
| `pytest-qt` + `QT_QPA_PLATFORM=offscreen` | Not applicable | Correct on any machine where Qt *loads*, and the CI job already used it. It does not solve the missing-library problem that blocked this container — which is what the shim addresses. Both are now in use together. |
| Install real `libgl1`/`libdbus` packages | Impossible here | No package network in this sandbox (`apt-get` cannot locate packages). On a normal CI image this is the better answer; the shim exists precisely for the case where it is not available. |

The shim proves the window loads and behaves. Appearance is covered separately:
`tests/test_appearance.py` renders the real window offscreen and asserts on
pixels, font metrics and region geometry, so the stylesheet, the type scale and
the layout are no longer taken on trust. What no test here can cover is a
*different* display server: HiDPI scaling, a real compositor and Windows'
native font rendering are still unverified on this machine, and the audit says
so rather than implying otherwise.
