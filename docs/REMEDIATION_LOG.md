# Defect hunt — verified findings

34 defects found and fixed, each re-verified by an executable probe, plus one
candidate investigated and **rejected as sound** (see the end). Every entry below
names the observable symptom, the fix and the check that proves the fix.

| # | Area | Defect (one line) |
|---|---|---|
| D1 | scheduler | offline-blocked task re-reported every tick, never advances |
| D2 | scheduler | runner exception escaped `tick()`; row stuck `pending` wedges the queue |
| D3 | scheduler | expiry boundary `now > expires` should be `>=` |
| D4 | parser | 7 everyday file ops (`copy`/`move`/`archive`/…) unreachable offline |
| D5 | reminders | past `today <time>` parsed to a time already gone |
| D6 | reminders | ISO `T` separator / trailing `z` rejected |
| D7 | redaction | 5 credential families not matched |
| D8 | redaction | Bearer floor 20 chars; Basic/Digest/Cookie never matched |
| D9 | redaction | nested `Authorization` **key** not caught |
| D10 | comms | reminders never fired; `repeat_interval` was dead; list showed past ones |
| D11 | memory | `consolidate` used salted `hash()` → unstable key across processes |
| D12 | memory | `consolidate` non-idempotent → duplicate row per run |
| D13 | memory | `search()` returned records that `retrieve_relevant` already hid as expired |
| D14 | web_search | DDG parser buffered 4,999 results from one hostile page |
| D15 | scraper | `TableParser` built 21,322 rows / 124 MB from a 5 MB page |
| D16 | scraper | dead `if …: pass` branch in `handle_endtag` |
| D17 | scraper | `PriceParser` could never capture a price without a currency symbol |
| D18 | memory | `record_episode(evidence=…)` accepted evidence and dropped it |
| D19 | file_wizard | `convert_file(fmt=…)` accepted a format and ignored it |
| D20 | research | `deep_research(max_sources=-5)` returned results (negative slice) |
| D21 | scraper | `scrape_tables(max_tables=-1)` showed 3 of 4 tables |
| D22 | file_manager | `read_text(max_chars=-1)` returned 99 of 100 chars |
| D23 | workspace | `read_source(max_chars=-1)` returned 3,888 of 3,889 chars |
| D24 | web_search | `extracted_text(max_chars=-1)` returned 4,999 of 5,000 chars |
| D25 | file_wizard | `analyze_storage(top_n=-1)` dropped a row and claimed truncation |
| D26 | file_manager | `search(max_results=0)` returned exactly one match |
| D27 | comms | negative SQL `LIMIT` returned the **whole table** from 4 queries |
| D28 | security | tool failure text reached the user **unredacted** (the last copy of a secret) |
| D29 | system_tuner | second backup of the same folder within one clock second failed outright |
| D30 | system_tuner | backing up into the source archived the live zip into itself, nesting every run |
| D31 | dev_assistant | `git_status` crashed on a repo with **no commits yet** |
| D32 | dev_assistant | git failures printed an empty reason (stderr was never captured) |
| D33 | dev_assistant | an unborn repo's branch was reported as `"No"` |
| D34 | dev_assistant | workspace manager was never injected, so every `workspace=<id>` call failed |

---

## D1–D13 (first pass)
1. D1 scheduler: offline-blocked task re-reported every tick, never advances  FIXED
2. D2 scheduler: runner exception escapes tick(); row stuck pending -> wedges queue  FIXED
3. D3 scheduler: expiry boundary `now > expires` → `now >= expires`  **FIXED**
4. D4 parser: 7 everyday file ops unreachable offline  FIXED
5. D5 reminders: past `today <time>` now rolls to next occurrence (+86400)  **FIXED**
6. D6 reminders: ISO `T` separator / trailing `z` accepted  **FIXED**
7. D7 redaction: 5 credential families not matched  FIXED
8. D8 redaction: Bearer floor 20 chars, Basic/Digest/Cookie never matched  FIXED
9. D9 redaction: nested Authorization key (not just value)  FIXED
10. D10 comms: reminders never fired; repeat_interval dead; list showed past ones  FIXED
11. D11 memory: consolidate used salted hash() -> unstable key across processes  FIXED
12. D12 memory: consolidate non-idempotent -> duplicate procedural row per run  FIXED
13. D13 memory: search() returned expired records that retrieval already hid  FIXED

## D14 — DDG parser accumulated unbounded results (memory amplification)
- **Evidence:** a 200 KB page of `result__a` anchors made `_DuckDuckGoHTMLParser` hold **4,999** full `WebSearchResult` objects; callers only keep `.results[:limit]` (≤12) *after* the whole page is parsed.
- **Fix:** `MAX_RESULTS = 50` class constant; `_flush()` stops appending past it.
- **Verified:** 5000 anchors → 50 results; normal nested-title page still parses correctly.

## D15 — TableParser buffered every table/row from a hostile page
- **Evidence:** a 5 MB table produced **21,322 rows** and drove RSS to **124 MB**; nothing limits tables/rows per document.
- **Fix:** `max_tables = 10`, `max_rows = 500`; extra tables are ignored, extra rows dropped.
- **Verified:** same page → 500 rows, RSS 48 MB; small table headers/rows unchanged.

## D16 — Dead `if` branch in `TableParser.handle_endtag`
- **Evidence:** `if not self._cur_table["headers"] and all(self._in_th or h for h in text_row): pass` — an AST scan for pass-only branch bodies found it; the condition computes `all(...)` over strings (always truthy) and then does nothing.
- **Fix:** branch removed; the live heuristic below it is unchanged.
- **Verified:** header/row extraction identical.

## D17 — `PriceParser` never captured prices without a currency symbol
- **Evidence:** body of the bare-number `re.finditer` loop was `if ...: pass`, so `$49.99` matched but `Price: 1,299.00 USD` and `49.99` returned `[]`.
- **Fix:** bare amounts are captured when the same text carries a price cue (`price|cost|total|now|only|from|mrp|was`) or a colon; ratings/versions stay ignored.
- **Verified:** `Price: 1,299.00` → `1,299.00`; `4.5 stars from 120 reviews` and `Version 2.1.30` → `[]`.

## D18 — `Memory.record_episode(evidence=...)` silently discarded evidence
- **Evidence:** `evidence` was accepted (keyword-only, default `""`) and never referenced; ruff `ARG002` flagged it. Callers could not audit an episode's proof.
- **Fix:** evidence folded into the persisted summary (`Evidence: …`), then redacted/secret-scanned as usual.
- **Verified:** `sha256:abc123` present in `memory_records.content`; absence of evidence leaves record unchanged; `Authorization: Bearer …` still raises `MemoryPolicyError`.

## D19 — `FileWizard.convert_file(fmt=...)` was accepted and ignored
- **Evidence:** `fmt` never read (ruff `ARG002`); `convert_file("note.md", "plain", fmt="html")` failed with "No converter for .md -> ." instead of producing `plain.html`. Also absent from the action schema, so it was an inert public parameter.
- **Fix:** explicit `fmt` (≠ `auto`) is honoured when the destination has no extension (path gets the extension appended); a conflicting known extension now raises instead of writing mismatched content.
- **Verified:** `fmt="html"` + extensionless dest → `plain.html` via `md->html`; `fmt="html"` + `.json` dest → `AutomationError` and no file written; `fmt` omitted → unchanged behaviour.

**Regression tests added:** `tests/test_web_search_and_polyglot.py::HostilePageParsingTests` (5), `tests/test_file_wizard.py::ConvertFileFormatTests` (3), `tests/test_memory_and_security.py::EpisodeEvidenceTests` (3).

## D20 — `deep_research(max_sources=negative)` produced results instead of refusing
- **Evidence:** the action schema bounds `max_sources` to 1..20 and `ActionValidator` rejects `-5`, but the *library* method shipped no guard, and negative slicing silently does the opposite of "fewer": `deep_research("x", max_sources=-5)` returned 1 result (all-but-the-last-five).
- **Fix:** clamp `max_results_per_query` to 1..10 and `max_sources` to 1..20 inside `deep_research` so direct callers get the same contract as the tool path.
- **Verified:** `-5`/`0` → 1, `99`/`10**6` → bounded, default unchanged.

## D21 — `SmartScraper.scrape_tables(max_tables=negative)` reported "Showing 3" for 4 tables
- **Evidence:** `parser.tables[:-1]` on a 4-table page returned 3 tables; `max_tables=-1` therefore showed *most* tables, and the summary line contradicted the payload.
- **Fix:** `parser.tables[:max(0, int(max_tables))]`.
- **Verified:** `max_tables=-1` → `tables == []`, `table_count` still 4, summary "Showing 0"; `max_tables=2` unchanged.

**Checked and cleared (no defect):** `memory.search(limit)` and `retrieve_relevant(limit)` already clamp; `price_parser` SIM102 was only a style nit; `_run_authorized_tool` raising `AutomationError` is correct because the production path (`workers.execute`) is wrapped in the recovery boundary at `orchestrator.py:1085`.

**Regression tests:** 2 added in `tests/test_web_search_and_polyglot.py::ResearchAndScrapeBoundsTests`.

## D22 — `FileManager.read_text(max_chars=-1)` returned the whole file minus one char
- **Evidence:** `read_text("a.txt", max_chars=-1)` on a 100-char file returned **99** chars with `truncated=True`; `-100` returned 0. `list_directory` next to it *does* clamp (`max(1, min(max_entries, 200))`), so this was an inconsistency as well as a wrong result.
- **Fix:** `max_chars = max(0, min(int(max_chars), self.config.max_read_bytes))`.
- **Verified:** `-1`/`-100` → 0 chars; `5` → 5; huge value → 100, not truncated.

## D23 — `WorkspaceManager.read_source(max_chars=-1)` returned 3,888 of 3,889 chars
- **Evidence:** same negative-slice shape; `-500` returned 3,389 chars.
- **Fix:** clamp to `0..500_000` (the module's own read ceiling) before the truncation test, so `truncated` also reflects the clamped bound.
- **Verified:** `-1`/`-500` → 0 chars, `truncated=True`; 100 → 100; huge → full file, `truncated=False`.

## D24 — `_ReadableHTMLParser.extracted_text(max_chars=-1)` returned 4,999 of 5,000 chars
- **Evidence:** negative bound sliced from the end; `-100` → 4,900.
- **Fix:** `max_chars = max(0, int(max_chars))` at the top.
- **Verified:** `-1`/`-100` → empty string; 50 → 50.

## D25 — `FileWizard.analyze_storage(top_n=-1)` contradicted its own request
- **Evidence:** `largest[: -1]` dropped the *last* (i.e. smallest) entry instead of showing none; the summary still reported the full file count.
- **Fix:** `largest[: max(0, top_n)]`.
- **Verified:** `top_n=-1`/`-3` → `[]`, `top_n=2` → 2 rows.

**Checked and cleared:** `find_duplicates(max_groups)` uses a `>=` counter (negative simply stops immediately, no wrong output); `_keystream(length)` is internal with a computed length; the remaining `_search_*` `limit` call sites are private and always passed schema-clamped values.

**Regression tests:** `ReadTextBoundTests` (1), `AnalyzeStorageBoundTests` (1), plus an added assertion in `test_coding_rag_and_security.py` for `read_source`.

## D26 — `FileManager.search(max_results=0)` returned one match
- **Evidence:** the bound is tested *after* `matches.append(...)`, so `max_results=0` (or any negative) returned exactly 1 hit with `truncated=True`; verified 1 match for 0, -1 and -5.
- **Fix:** early return `{"matches": [], "truncated": False, "directories_searched": 0}` when `max_results <= 0`.
- **Verified:** 3→3, 2→2, 1→1, 0/-1/-5→0.

## D27 — negative `LIMIT` returned the whole table from four communication queries
- **Evidence:** SQLite treats `LIMIT -1` as "no limit". `list_drafts(limit=-1)` and `list_reminders(limit=-1)` returned all 5 rows; `list_reminders(limit=-1, pending_only=False)` likewise; `due_reminders(limit=-1)` shares the pattern.
- **Fix:** clamp every `LIMIT ?` binding with `max(0, int(limit))`.
- **Verified:** limit 3→3, 1→1, 0→0, -1/-100→0 for drafts, reminders (both modes) and due.

**Swept and cleared:** all other `LIMIT ?` sites already clamp — `workers.events` `max(1,min(limit,100))`, `memory.recent_messages`/`search`/`consolidate` (`max(1,min(...))`, `max(2,...)`), `observability.recent_runs`/`snapshot`, `knowledge.index` `max(10,min(limit*4,100))`, `tasks.store` `max(1,min(limit,100))`. `list_directory` and `inventory` already clamp via `max(1,min(...))`.

**Regression tests:** `ReminderAndDraftLimitTests` (3) + `test_non_positive_search_limit_returns_no_matches` (1).

## REJECTED CANDIDATE — `IntentParser.parse()` raising `ValidationError`
- **Apparent symptom:** `parse("open " + "x"*5000)` raised `ValidationError("name must be a string of length 1..64")` instead of returning `None`, contradicting the `ActionRequest | None` signature.
- **Why it is NOT a defect:** the raise is deliberate and load-bearing. `"set volume to 900"` must raise so the user is told the value is invalid rather than being silently told "I don't understand". Three tests encode this contract (`test_volume_validation`, `test_router_calls_every_section`, `test_commands_are_distinct_from_local_search`).
- **Attempted fix and reversal:** wrapping `parse()` in `try/except ValidationError: return None` broke all three and downgraded a precise "Invalid command: volume value must be between 0 and 100" into "I need a clearer request". Reverted; the orchestrator's existing `except ValidationError` at line ~300 is the correct handling layer.
- **Lesson:** a signature's `| None` does not imply "never raise" when a sibling contract (validation feedback) depends on the raise. Check the tests before "fixing" an exception.

## D28 — user-visible tool failure messages were the one unredacted copy of a secret
- **Evidence:** `process_text("read file notes sk-proj-ABCDEF…txt")` returned
  `Path does not exist inside permitted folders: notes sk-proj-ABCDEF…txt`.
  Conversation history was clean (`[REDACTED_SECRET:openai_key]`) because
  `memory.add_message` redacts, so the *response* — what the user sees, copies
  and screenshots — was the only remaining copy of the credential.
- **Fix:** `ActionResult.failure(action.tool, redact_text(str(exc)))` at both
  recovery boundaries in `_execute` (`AutomationError`/`OllamaError`/
  `ClassifiedError` and the last-resort `Exception` branch).
- **Verified:** four failure paths that echo user text (`read file`, missing
  `convert`/`copy` sources, credential-bearing filenames) now return
  `[REDACTED_SECRET:openai_key]`; history clean; ordinary messages unchanged
  (`definitely_missing_file.txt` still named).
- **Checked and cleared:** `ValidationError` messages never embed the value
  (`"path must be a string of length 1..1024"`), so the other `AssistantResponse(f"…")`
  call sites cannot leak by that route.

**Regression tests:** `FailureMessageRedactionTests` (3) in `tests/test_dispatch_authorization.py`.

## D29 — two backups of the same folder in one second: the second failed outright
- **Evidence:** the archive name is `backup_<name>_<YYYYmmdd_HHMMSS>.zip`; a second `backup_create` within the same clock second raised `AutomationError("Backup dest exists: …")`. A scheduled backup plus a user request, or a retry, hits this.
- **Fix:** on collision, probe `_2`, `_3`, … up to `_999` and use the first free name; only fail if all are taken.
- **Verified:** 4 back-to-back calls in one second produce 4 distinct archives, all present.

## D30 — backing up into the source archived the live archive into itself
- **Evidence:** `backup_create("Documents", "Documents")` produced
  `['x.txt', 'backup_…_190920.zip']` — the partially written zip being added to itself. Each run then nested the previous backups: 115 B → larger → larger. The code comment claimed the walk was pruned, but only the destination **directory** was excluded, and the archive sits in that directory.
- **Why the existing test missed it:** `test_backup_never_includes_previous_backups` sleeps 1.05 s between runs, so the two archives have different names and never collide within one walk.
- **Fix:** skip the exact file being written (`fp.resolve() == dst.resolve()`) and, separately, any existing `backup_<src.name>_*` archive that lives in the destination directory.
- **Verified:** source==destination → three 115 B archives, each containing only `x.txt`; destination inside source → `['data/a.txt']` only; ordinary separate-destination backup unchanged.

**Checked and cleared:** `quick_cleanup` already clamps (`max(0, min_age_days)`) and refuses symlinks.

**Regression tests:** `SameSecondBackupTests` (2).

## D31 — `git_status` crashed on a repository with no commits yet
- **Evidence:** after `git init` + config, `git_status` raised `AutomationError("git status failed in …: Command '[...git log --oneline -5]' returned non-zero exit status 128")`. `git log` legitimately fails on an unborn branch. A brand-new project is the first thing a user points this at.
- **Fix:** tolerate `CalledProcessError` from `git log` and report `log: []`.
- **Verified:** fresh repo → `changed_files=1`, `log=[]`, no exception.

## D32 — git failures printed no reason at all
- **Evidence:** `git_commit` on an untracked-only repo produced `AutomationError("git commit failed: ")` — empty. `str(CalledProcessError)` is "Command '[...]' returned non-zero exit status N", but the code interpolated `exc.output`, which is `""` because the original calls did not capture stderr.
- **Fix:** capture stderr on all five guarded `check_output` calls (`stderr=subprocess.STDOUT`) and route messages through a new `_git_failure()` helper that prefers git's own `fatal:`/`error:`/`warning:` line.
- **Verified:** three failure paths now report `fatal: not a git repository (or any of the parent directories): .git` (and `warning: Not a git repository…` for `git diff`), instead of an empty or usage-block tail.

## D33 — a fresh repo's branch was reported as `"No"`
- **Evidence:** git prints `## No commits yet on trunk` on an unborn branch; the parser took `header.split(" ")[0]`, yielding `branch="No"`.
- **Fix:** recognise the `No commits yet on <branch>` header.
- **Verified:** `git init -b trunk` → `branch == "trunk"`; after the first commit, still `trunk`.

**Regression tests:** `GitErrorReportingTests` (3, skipped when git is absent).

## D34 — `DevAssistant` never received the workspace manager, so workspace ids never worked
- **Evidence:** `assistant.py:125` built `DevAssistant(files=…, workspace_manager=None)` with the comment *"workspace_manager injected via orchestrator"*. Nothing injects it: the orchestrator receives the **already-built** object (`if dev_assistant is not None: self.dev_assistant = dev_assistant`), so its own default branch never runs. Result: `assistant.dev_assistant.workspaces is None`, and `git_status`/`code_review`/`dependency_audit` all failed with `Workspace '<id>' is not inside an approved folder` for ids handed out by `register_workspace`.
- **Fix:** `assistant.py` now assigns `self.dev_assistant.workspaces = self.workspaces` right after constructing the `WorkspaceManager` (which happens on the following lines).
- **Verified:** `code_review(workspace=<registered id>)` scans the project (4 findings on a fixture with a bare except, a hardcoded key, a 200-char line and a missing docstring); `dependency_audit(workspace=<id>)` lists `requirements.txt`; the object is shared with `assistant.workspaces`.

**Regression tests:** `DevWorkspaceInjectionTests` (2).
