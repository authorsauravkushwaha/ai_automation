# HINATA weakness audit — round 2, 100 fresh findings

**Branch:** `arena/ba089f7a-ai-automation`
**Base commit:** `9940e48b2aa6ff34a4b8d5793b1ec5571ee586b8`
**Date:** 2026-11-26
**Scope:** the current working tree after the historical `docs/WEAKNESS_AUDIT.md` from
branch `arena/d5354f12-ai-automation` was already closed. This is a second-round
audit, not a copy of that document.

## Verdict

100 fresh concrete weaknesses or hardening gaps were selected from runtime probes,
targeted source inspection and regression-test failures. All 100 are now fixed,
safely constrained, or pinned by an executable regression test. The work added
**51 new regression tests**, raised the suite from **566 to 617 tests**, and raised
statement coverage from the historical **58% to 60%** while keeping the full ruff
and mypy gates green.

Severity below is an engineering-impact label, not a CVSS score: **H** means a
trust/security boundary, **M** means correctness or robustness, **L** means
provenance/maintainability. Several findings are low-severity hardening gaps;
none of the 100 is just a linter warning promoted without a probe.

## Freshness and overlap

The older audit covered five *different* credential classes, a DNS-offline bug,
a DevAssistant containment escape and a large test-coverage pass. Round 2 uses
different concrete token families, different SSRF call sites, PIN lockout state,
hostile config shapes, communication truthfulness, vault key creation races,
threaded-sandbox limits and bounded runtime inputs. Two earlier items were only
**flagged for review** rather than closed: the `fill_field` private-URL default
and the idea of stricter URL policy. Round 2 reproduced and closed those gaps
with new evidence; they are not counted as copies of the older closed findings.

## Method

1. Baseline gates were run before edits: 566 tests OK, ruff clean, mypy clean,
   coverage 58%.
2. Static discovery used broad ruff rules, Bandit-style `S` rules, targeted rule
   sets and an AST scan. These produced leads only: 1767 broad diagnostics, 198
   targeted diagnostics, 194 AST items and 94 `S` warnings at baseline.
3. Each candidate was triaged against source. Only candidates that could be
   reproduced by a runtime probe, a failing regression test, or a deterministic
   source-level check were promoted into the 100.
4. 51 regression tests in `tests/test_round2_regressions.py` pin the fixes.
   Representative executable probes include a local HTTP 302 to the metadata
   address, a mocked private DNS answer, invalid TOML loads, PIN failure/lockout
   timing, fake `Popen`/`prlimit` inspection, a fake URL opener that records
   `resp.read(n)`, and a config-less LLM stub.
5. Full gates were rerun after all edits.

## The 100 findings

| Cluster | Count |
|---|---:|
| `R2-001..013` — Credential families and central response redaction | 13 |
| `R2-014..025` — Public URL / SSRF / redirect policy | 12 |
| `R2-026..031` — PIN verification and lockout | 6 |
| `R2-032..039` — Online allowlist, URL policy and config validation | 8 |
| `R2-040..047` — Communication truthfulness and bounded reminders | 8 |
| `R2-048..058` — File wizard, vault key creation and sandbox runner | 11 |
| `R2-059..070` — Hostile configuration shapes | 12 |
| `R2-071..076` — Email header injection and reminder arithmetic | 6 |
| `R2-077..082` — Web-search download, timeout and slice bounds | 6 |
| `R2-083..089` — Research synthesis and external browser URL validation | 7 |
| `R2-090..100` — Backups, tool timeouts, clipboard, cancellation and shared bounds | 11 |

Every row below is **Fixed / pinned** unless explicitly described as a control
preserved by a test. Evidence refers to `tests/test_round2_regressions.py` or the
named module-level probe.

| ID | Sev | Area | Weakness at the round-2 baseline | Fix / evidence |
|---:|:---:|---|---|---|
| R2-001 | H | Redaction | GitLab personal-access tokens were not detected by the secret scanner. | Added provider rule; pinned by NewCredentialRedactionTests. |
| R2-002 | H | Redaction | npm automation tokens were not detected. | Added provider rule; pinned by NewCredentialRedactionTests. |
| R2-003 | H | Redaction | HuggingFace access tokens were not detected. | Added provider rule; pinned by NewCredentialRedactionTests. |
| R2-004 | H | Redaction | SendGrid-style API keys were not detected. | Added provider rule; pinned by NewCredentialRedactionTests. |
| R2-005 | H | Redaction | Telegram bot-token strings were not detected. | Added provider rule; pinned by NewCredentialRedactionTests. |
| R2-006 | H | Redaction | A Slack client-token family was outside the baseline rule. | Broadened Slack rule; pinned by NewCredentialRedactionTests. |
| R2-007 | H | Redaction | Slack app-level tokens were not detected. | Added dedicated rule; pinned by NewCredentialRedactionTests. |
| R2-008 | H | Redaction | Cloud storage key assignments and connection strings leaked key material. | Added key/connection-string redaction while preserving EndpointSuffix. |
| R2-009 | H | Redaction | URL credentials with an empty username were not covered. | Regex now allows empty usernames; pinned by NewCredentialRedactionTests. |
| R2-010 | H | Redaction | X-Auth-Token, CSRF/XSRF, session and Amazon security-token headers were not redacted. | Extended auth-header coverage; pinned by NewCredentialRedactionTests. |
| R2-011 | H | Response boundary | ActionResult.message could carry a raw credential from any tool. | ActionResult.__post_init__ redacts message and data. |
| R2-012 | H | Response boundary | AssistantResponse.text and data could bypass redaction on direct library calls. | AssistantResponse.__post_init__ redacts text and data. |
| R2-013 | M | Response boundary | A future global-redaction change could rewrite pending ActionRequest arguments before confirmation. | Pending action is deliberately unchanged; pinned by CentralResponseRedactionTests. |
| R2-014 | H | SSRF | is_safe_public_url accepted the cloud metadata literal 169.254.169.254. | Fail-closed literal-IP checks; PublicUrlPolicyTests. |
| R2-015 | H | SSRF | Hex integer hosts such as 0x7f000001 bypassed literal checks. | Numeric host forms rejected before socket use. |
| R2-016 | H | SSRF | Decimal integer hosts such as 2130706433 bypassed literal checks. | Numeric host forms rejected before socket use. |
| R2-017 | H | SSRF | URLs with embedded user:pass credentials were accepted as public candidates. | Userinfo is rejected by the shared policy. |
| R2-018 | H | SSRF | Out-of-range ports such as 99999 were not rejected deterministically. | Port parsing and bounds are fail-closed. |
| R2-019 | M | SSRF | Special-use suffixes such as printer.local were accepted. | Unsafe suffix list added; PublicUrlPolicyTests. |
| R2-020 | H | SSRF | A public hostname resolving to 10.0.0.5 was accepted without answer checks. | getaddrinfo answers are checked; pinned with a mocked DNS answer. |
| R2-021 | M | SSRF | Loopback was allowed by default in the URL helper. | allow_loopback defaults to False and is literal-only. |
| R2-022 | H | SSRF | normalize_url applied a weaker static policy than the search helper. | Shared ensure_public_fetch_url policy; PublicUrlPolicyTests. |
| R2-023 | H | SSRF | fetch_page used a plain opener without redirect-target revalidation. | build_public_web_opener validates every redirect; local 302 probe. |
| R2-024 | H | SSRF | inspect_url, autofill, fill_field and click_element defaulted to allow_private=True. | All now default to public-only unless explicitly opted in. |
| R2-025 | H | SSRF | SmartScraper.fetch accepted private targets and crawl_pages accepted negative budgets. | Public-only fetch, revalidated opener, clamped 20-page budget. |
| R2-026 | H | Authentication | PIN verification had no failure lockout or rate limit. | 5-attempt / 60-second settings-backed lockout added. |
| R2-027 | M | Authentication | Non-ASCII PIN input raised UnicodeEncodeError instead of simply failing. | ASCII/digit/length checks before PBKDF2. |
| R2-028 | M | Authentication | Non-digit and overlong PIN strings entered PBKDF2 unchecked. | Strict 4-12 ASCII digit validation. |
| R2-029 | M | Authentication | A hostile stored iteration count could request an extreme PBKDF2 work factor. | Stored envelope parsed with 100000-1000000 bounds. |
| R2-030 | M | Authentication | A hostile locked_until value could lock the owner out indefinitely. | Remaining lockout is capped at one 60-second window. |
| R2-031 | H | Authentication | lockout_remaining cleared ordinary failure counters on every failed attempt, so the fifth failure never locked. | Fixed during regression testing; set_pin and clear_pin also clear stale lockout. |
| R2-032 | H | Offline policy | allowed_online_domains accepted a bare TLD such as com. | validate_public_domain requires domain plus suffix. |
| R2-033 | H | Offline policy | Allowlist entries with a scheme or path were accepted. | Only bare DNS names are accepted. |
| R2-034 | H | Offline policy | Wildcard and leading-dot allowlist entries were accepted. | Ambiguous entries are rejected. |
| R2-035 | H | Offline policy | Literal private IP allowlist entries were accepted. | IP literals and special-use suffixes are rejected. |
| R2-036 | M | Offline policy | check_url accepted file:// URLs. | HTTP(S)-only scheme check added. |
| R2-037 | M | Offline policy | check_url accepted URL credentials and raised raw ValueError on an invalid port. | Credentials rejected; invalid port wrapped as OfflinePolicyError. |
| R2-038 | M | Config | WakeWordConfig threshold and cooldown accepted strings, NaN and out-of-range values. | Finite-number and range checks; ConfigShapeHardeningTests. |
| R2-039 | M | Config | InterfaceConfig booleans and SecurityConfig numeric/search fields were not shape-checked. | Strict bool/int/finite checks and suffix regex; DomainAllowlistTests. |
| R2-040 | M | Communication | _ensure_table used assert self.db, which disappears under python -O. | Explicit AutomationError replacement. |
| R2-041 | M | Communication | _parse_when used assert clock is not None, which disappears under python -O. | Explicit AutomationError replacement. |
| R2-042 | M | Communication | Identity-vault autofill failures were silently swallowed, producing placeholder drafts. | Visible redacted autofill_warning returned to caller. |
| R2-043 | M | Communication | create_reminder accepted negative repeat_seconds. | Finite non-negative interval validation. |
| R2-044 | M | Communication | create_reminder accepted NaN or infinite timestamps. | Finite epoch validation. |
| R2-045 | M | Communication | create_reminder accepted non-string titles, channels and times. | String and control-character checks. |
| R2-046 | M | Communication | broadcast_<epoch>.txt could overwrite a same-second notification. | UUID suffix added; pinned by CommunicationTruthfulnessTests. |
| R2-047 | M | Communication | Local broadcast write errors and hostile channel lists were invisible. | local_file_written/error fields and channel validation added. |
| R2-048 | M | File wizard | Duplicate detection used MD5 for quick and full file hashes. | SHA-256 used for both; hash_algorithm exposed in the result. |
| R2-049 | L | File wizard | find_duplicates(max_groups=-1) sliced from the end and bypassed the group ceiling. | Group limit clamped to 0-50. |
| R2-050 | L | File wizard | find_duplicates(min_size_kb=-1) made the size floor negative. | Size floor clamped to zero. |
| R2-051 | L | File wizard | Duplicate results did not state which digest algorithm produced the hash. | hash_algorithm field added. |
| R2-052 | H | Local vault | Two processes creating the initial vault key could replace each other's key with write_private. | O_EXCL atomic creation converges on one key. |
| R2-053 | H | Local vault | Initial key creation did not fsync or enforce 0600 on POSIX before publication. | Exclusive descriptor, fsync and chmod before use. |
| R2-054 | H | Local vault | Two LocalVault instances could diverge after a create race. | Second instance reads the winning key; VaultKeyCreationTests. |
| R2-055 | H | Sandbox | preexec_fn used os.setsid and rlimits inside a threaded desktop process. | start_new_session plus parent-side prlimit on Linux. |
| R2-056 | M | Sandbox | Sandbox TEMP/TMP fell back to a hardcoded /tmp path. | tempfile.gettempdir is used; pinned by SandboxRunnerHardeningTests. |
| R2-057 | M | Sandbox | Platforms without prlimit had no equivalent post-spawn resource limits. | Fallback _preexec applies the same CPU/AS/NOFILE limits. |
| R2-058 | M | Sandbox | Windows fallback Popen did not use the same session flags as the primary path. | Both Popen paths now share start_new_session and limit application. |
| R2-059 | M | Config | OllamaConfig booleans accepted non-boolean TOML values. | _require_bool validation added. |
| R2-060 | M | Config | OllamaConfig integer bounds accepted bools and floats. | _require_int validation added. |
| R2-061 | M | Config | OllamaConfig temperature accepted NaN and out-of-range values. | Finite 0-2 range enforced. |
| R2-062 | M | Config | OllamaConfig keep_alive accepted negative integer seconds below -1. | -1 or non-negative only. |
| R2-063 | H | Config | Loopback host URLs accepted invalid ports, paths and userinfo. | _require_loopback_http_url validates scheme, host, port, path and credentials. |
| R2-064 | M | Config | AgenticConfig integer bounds accepted bools and strings. | _require_int validation added. |
| R2-065 | M | Config | AgenticConfig duration and per-tool timeout accepted NaN/inf. | _require_finite_number validation added. |
| R2-066 | M | Config | SecurityConfig max_read_bytes and search_max_directories accepted bools/floats. | _require_int validation added. |
| R2-067 | M | Config | SecurityConfig.allowed_roots accepted non-path entries and could raise raw TypeError. | Type checks and ConfigError wrapping added. |
| R2-068 | M | Config | SpeechConfig bool-like tts_rate and microphone values were accepted. | Bool rejection and int/string checks added. |
| R2-069 | M | Config | WakeWord, Interface, Memory and Personality sections had inconsistent hostile-shape handling. | Unified bool/int/finite/string checks; ConfigShapeHardeningTests. |
| R2-070 | M | Config | Application command and process_names accepted non-string or empty parts. | All parts validated as non-empty strings. |
| R2-071 | H | Email draft | To headers containing CRLF or NUL could be stored in the DB and returned to callers. | Header injection rejected before persistence. |
| R2-072 | H | Email draft | Subject and From header values could contain CRLF or NUL. | All header values validated at the boundary. |
| R2-073 | M | Email draft | draft_email accepted non-string to, subject, body and path values. | All draft fields must be strings. |
| R2-074 | M | Email draft | template_vars accepted non-string values that later reached format/headers. | String key/value validation added. |
| R2-075 | M | Reminders | fire_due_reminders could spin for years of simulated missed occurrences with a sub-second repeat interval. | 60-second floor plus bounded arithmetic skip. |
| R2-076 | M | Reminders | due_reminders and fire_due_reminders accepted NaN/inf timestamps and unbounded limits. | Finite epochs and 0-200 limit clamps. |
| R2-077 | M | Web search | _http_get(max_bytes=-1) called resp.read(-1), disabling the download cap. | max_bytes clamped to 0-MAX_DOWNLOAD_BYTES. |
| R2-078 | M | Web search | _http_get(max_bytes=huge) could request more than MAX_DOWNLOAD_BYTES. | Upper bound enforced; fake-opener regression test. |
| R2-079 | M | Web search | WebSearchEngine(timeout_seconds=NaN) produced an invalid timeout. | _clamp_float falls back to 6 seconds. |
| R2-080 | M | Web search | Provider methods sliced with negative limits, returning all but the last results. | All provider limits clamped to 0-12. |
| R2-081 | M | Web search | fetch_webpage(max_chars=NaN) raised raw ValueError. | _clamp_int returns the 4000-character default. |
| R2-082 | M | Web search | search(max_results=NaN) raised raw ValueError. | _clamp_int returns the default of 8 results. |
| R2-083 | M | Research | _make_queries called strip on a non-string topic. | Explicit AutomationError. |
| R2-084 | M | Research | Malformed search results missing title, url or snippet raised KeyError. | _result_text tolerates missing keys. |
| R2-085 | M | Research | compare_topics and summarize_url assumed llm.config existed and crashed on config-less LLM stubs. | _llm_enabled uses a safe getattr chain. |
| R2-086 | M | Research | LLM synthesis, comparison and summary failures were silently replaced with fallback text. | Redacted synthesis_error/comparison_error/summary_error fields. |
| R2-087 | H | Browser launch | open_browser_search accepted file:// URLs. | Only HTTP(S) is allowed. |
| R2-088 | H | Browser launch | Private, loopback, link-local and .local browser URLs were accepted. | Literal-IP and special-suffix checks added. |
| R2-089 | H | Browser launch | Credentialed URLs, invalid ports and non-string browser names were accepted. | Credential/port/type validation added; BrowserUrlValidationTests. |
| R2-090 | M | Backups | backup_create(max_age_days=-1) moved the cutoff into the future and pruned every existing backup. | Non-negative 0-3650 validation before archive creation. |
| R2-091 | M | Tool registry | timeout_override=0 was silently replaced by the manifest timeout because of or. | Explicit None check and positive-timeout validation. |
| R2-092 | H | Tool registry | Negative, NaN, too-large or bool timeout overrides could bypass the timeout pool entirely. | ClassifiedError before handler execution; ToolRegistryTimeoutTests. |
| R2-093 | M | Clipboard | read_clipboard scanned and redacted an unbounded clipboard string before slicing the result. | Input is bounded first; length and truncated flag returned. |
| R2-094 | M | Clipboard | write_clipboard called len on non-string input. | String type check added. |
| R2-095 | M | Cancellation | Cancellation callback failures were silently swallowed in cancel(). | Redacted LOGGER.error diagnostic added. |
| R2-096 | M | Cancellation | Immediate on_cancel callback failures were silently swallowed. | Same redacted logging path; ClipboardAndCancellationHardeningTests. |
| R2-097 | M | LLM client | Ollama _request and chat accepted NaN/negative timeout values. | _bounded_timeout clamps to sane ranges. |
| R2-098 | M | Ollama service | probe, port-open and model-pull timeouts accepted NaN/negative values. | _bounded_timeout clamps all three paths. |
| R2-099 | M | Sandbox runner | run_command accepted NaN/negative timeout and memory values before int conversion. | Shared clamps applied before CPU/AS limits and communicate. |
| R2-100 | M | Sandbox runner | Shared clamp helpers had no regression coverage for hostile values. | RuntimeBoundHelperTests pins finite fallback behavior. |

## Verification after the changes

```text
.venv/bin/ruff check hinata tests scripts -> All checks passed!
.venv/bin/mypy hinata                    -> Success: no issues found in 86 source files
python scripts/run_tests.py              -> Ran 617 tests in 43.417s, OK (skipped=52)
python -m unittest tests.test_round2_regressions -> Ran 51 tests, OK
coverage run --source=hinata ...         -> 15644 Stmts, 6295 Miss, 60%
```

The historical baseline was 566 tests and 58% coverage. The 52 skips are
platform/display-dependent GUI, Windows and optional-hardware tests; they are not
silently treated as passes.

## Residual static leads — deliberately not counted as findings

After the fixes, `ruff check hinata --select S --output-format concise` still
reports **78 static leads**, down from **94** at baseline:

```text
30 S603   subprocess with checked argv      20 S110  optional-hardware exception swallows
 8 S607   partial executable path           7 S310  loopback/public opener calls
 4 S608   dynamic SQL from internal names   3 S606  OS launcher calls
 2 S314   XML with explicit DOCTYPE bans    2 S311  deterministic ANN/star RNG
 2 S112   exception-continue loops
```

These were triaged but not promoted into the 100. Examples: `S608` dynamic SQL is
built from internal table/field whitelists; `S314` XML paths already reject
DOCTYPE/ENTITY declarations; `S311` random generators are deterministic visuals
or ANN planes, not cryptographic secrets; `S310` openers are loopback-only or
redirect-revalidated public openers; many `S110` sites are optional audio/GPU
fallbacks. They remain useful review leads, not verified defects.

## Honesty boundaries

- Mocks and fake openers validate Python-level policy and control flow. They are
  **not** validation of real hardware, a real Ollama model, or a real provider API.
- Python-level offline guards and URL checks are **not** an OS firewall. The
  local 302 test proves redirect revalidation inside HINATA, not host networking.
- Windows-only Job Object, voice-profile, PowerShell and GUI behavior cannot be
  executed on this Linux sandbox; 52 skipped tests are reported, not hidden.
- No external provider was contacted for the credential/SSRF probes. Public-host
  checks were deterministic or DNS-mocked.
- 60% coverage is measured but is not proof of absence of defects. Low-coverage
  GUI, speech and image modules remain the highest-value future test targets.
- The older audit and status docs contain historical branch names and are not
  current branch state.

## Deliverables

- `docs/WEAKNESS_AUDIT_ROUND_2.md` — this report.
- `tests/test_round2_regressions.py` — 51 executable regression tests.
- 23 modified source files across redaction, URL policy, authentication,
  config, communication, file operations, local vault, sandbox, research,
  browser launch, tool registry, clipboard, cancellation and scheduler logging.
