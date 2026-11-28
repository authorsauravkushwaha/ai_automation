# Ultron audit remediation — working-tree status

Date: 2026-11-27. Baseline: [100-finding audit](ULTRON_WEAKNESS_AUDIT.md).

**This is a substantial remediation pass, not a claim that all 100 findings are fixed.**
Ultron remains enabled by default as requested. Explicit saved policy, confirmation,
PIN, lockdown, and saved-job boundaries remain in force. Actual Chromium and Qt offscreen validation now pass on Linux. Windows, real-model
and physical hardware validation are recorded separately; see the latest platform evidence.

## Follow-up: additional safeguards

[Implementation details, compatibility changes, migration and validation commands](ULTRON_HARDENING.md).
The second pass adds connect-time public-IP pinning, same-origin browser mediation,
subprocess quotas/resource bootstrap, versioned AES-GCM writes and explicit backup-first
migration, and independent move/archive/patch verification. It also adds 29 portable
regressions, an opt-in actual Chromium test, and a versioned real-model evaluation harness.
The initial checkpoint downloaded no model/browser binaries. Subsequent actual
validation explicitly prepared Chromium and a synthetic-evaluation model; no private
user data was downloaded or migrated.

## Validation

| Environment / command | Discovered | Passed | Skipped | Duration |
|---|---:|---:|---:|---:|
| Core only: `python scripts/run_tests.py` | 743 | 679 | 64 | 55.236s |
| With cryptography 48.0.1: `.venv/bin/python scripts/run_tests.py` | 743 | 687 | 56 | 62.693s |

Both runs completed **OK**. The first pass added 32 regression tests; this follow-up
adds 29 portable tests and one opt-in actual Chromium test. Core-only crypto skips
are separately exercised in the secure-storage environment. Actual browser, Windows,
GUI and real-model acceptance are not represented as passed by these results.

- `.venv/bin/ruff check hinata tests scripts`: passed.
- `.venv/bin/mypy hinata`: passed, 92 source files.
- `git diff --check`: passed.
- Python compilation: passed as part of the test runner.
- CI now requests Python 3.11–3.14 on Linux and Windows for the core job, and pins
  checkout/setup-python to verified upstream tag SHAs. Subsequent remote execution
  is documented in [the platform report](ULTRON_PLATFORM_VALIDATION.md). Only
  Python 3.11 was exercised locally.
- `constraints-dev.txt` records the locally tested quality-tool versions. This is
  **not** a full runtime or optional-dependency lockfile.
- The previous 61% coverage measurement is historical; no new coverage percentage
  is claimed.

### Latest actual-platform validation

The combined suite now runs **747 tests: 742 passed and 5 skipped, 71.528s**,
including **33 actual Qt tests** and **actual Chromium acceptance**. The earlier
rows above are historical checkpoints, not latest totals. Actual Windows CI found
and prompted fixes to process lifetime, backup flushing and DPAPI binary writes.
**All 15 final CI jobs passed**, including Windows/Linux Python 3.11–3.14,
Windows Qt, hardening and actual Chromium. The separate Windows launcher job passed.

A real Qwen 0.5B/Ollama run completed all 16 evaluations without runtime/engine
errors: **off 6/8 versus ultra 5/8**, with ultra slower. This is execution evidence,
**not an accuracy-improvement claim**. See the [full platform report](ULTRON_PLATFORM_VALIDATION.md),
[platform JSON](audits/ultron-platform-validation.json), and
[complete model results](audits/ultron-real-model-validation.json).

### Probe evidence is not a test-pass count

`docs/audits/ultron-probes.json` is the unchanged historical baseline (36 reproduced
weaknesses). `docs/audits/ultron-remediation-probes.json` is the first-pass rerun of that
**unchanged pre-fix harness** (it predates this additional hardening): 27 `false`, 8 exceptions, 1 `true`. The harness exits
1 because it expects weaknesses to reproduce; this is expected, not a green test.

Eight probes now encounter deliberate rejection exceptions; exceptions alone are
not proof of correctness. Dedicated regression tests assert the relevant rejection
and no-side-effect behavior. The remaining `schedule_budget=true` is a stale probe:
it mocks `process_text` and equates entry into that method with tool execution.
The call now carries a zero-tool `BudgetTracker`. The real execution path is covered
by `test_scheduled_zero_tool_budget_blocks_dispatch`, which asserts the dispatcher
is never called. Do not describe this raw JSON as “36 security tests passed.”

## Status meanings

- **Implemented**: the specific defect has a code change, with the portable suite
  green. Evidence below distinguishes targeted regression tests from source review;
  this does not certify the whole surrounding subsystem.
- **Partial**: mitigation exists, but part of the finding or its assurance requirement
  remains unresolved.
- **Open**: no sufficient remediation in this pass; required follow-up is stated.
- **Validation pending**: implementation/CI coverage was added, but the required
  environment has not been exercised.

Evidence abbreviations: **R** = `tests/test_ultron_remediation.py`; **U** =
`tests/test_ultron_automation.py`; **T** = `tests/test_reasoning_engine.py`;
**DAG** = existing planner/replanning/agentic tests; **H** =
`tests/test_ultron_hardening.py`. Paths are current paths; the
baseline audit's line numbers intentionally remain historical.

**Status totals:** 62 implemented, 0 validation pending, 27 partial, 11 open.

| Finding | Status | Change, evidence, or remaining work |
|---|---|---|
| U001 | Implemented | Explicit loaded thinking modes take precedence; saved `off` is preserved. `config.py`, `assistant.py`; R saved-policy test. |
| U002 | Implemented | Explicit autonomy and stricter direct-config policies survive startup. R/U policy tests. |
| U003 | Implemented | Repeated on reapplies the effective preset; status exposes modifications. `assistant.py`; U controls. |
| U004 | Implemented | Off restores only fields still equal to the applied preset; intervening edits survive. R owner-edit test. |
| U005 | Implemented | Visible desktop on/off button and autonomy dialog added. `interface/main_window.py`; 33 actual Qt tests pass on Linux; default toggle and disabled inference are covered. |
| U006 | Partial | Status explicitly says model readiness is not checked; legacy availability flag is still configuration-based. Live readiness remains separate. |
| U007 | Implemented | Effective thinking enablement now includes the inference-enable flag. R/T no-LLM status checks. |
| U008 | Implemented | Loaded `source_path` reaches persistence. R custom-config-path test. |
| U009 | Implemented | Single-quoted values are replaced and resulting TOML/value is verified. R persistence test. |
| U010 | Implemented | Indented mode keys are recognized; invalid/unsupported resulting TOML is rejected. R persistence test. |
| U011 | Partial | Unique temporary + fsync + atomic replacement + concurrent-content check. No cross-process settings lock; final check/replace race remains. |
| U012 | Open | Default-on preserved. No hardware-aware routing benchmark or adaptive quality evidence yet. |
| U013 | Implemented | Validated overrides are clamped to configured limits and passed through a shared tracker. R real zero-tool dispatch test. |
| U014 | Implemented | Requests/titles exceeding 2000/160 characters are rejected. R scheduler validation. |
| U015 | Implemented | Real time refreshed per claim; expiry is included in atomic SQL claim. Scheduler source review and updated probe. |
| U016 | Partial | Bounded runner wait, disabled unknown attempt, and local active-worker gate. R deadline test. No forced thread termination/machine-wide supervisor. |
| U017 | Implemented | Fixed-delay recurrence uses completion time; no catch-up burst. `scheduler.py`; documented semantics. |
| U018 | Open | Watcher remains foreground-only. No owner-installed OS service integration added. |
| U019 | Implemented | Missing expiry defaults to 30 days after initial schedule time. R expiry test; explicit owner expiry remains honored. |
| U020 | Implemented | Strict boolean validation at scheduler boundary. R bad-type test. |
| U021 | Implemented | Interval bounded to 0–31536000 seconds, integer only. R overflow test. |
| U022 | Implemented | Paginated list and ID lookup in API/CLI. R lookup/pagination test. |
| U023 | Implemented | Migration 8 adds per-attempt start/end/status/task/review history; claim and attempt insert share a transaction. R attempt tests. |
| U024 | Partial | Redacted pending tool/arguments/reason are retained in attempt review. Not a sealed/resumable approval artifact; requires fresh approval. |
| U025 | Implemented | Tick returns the stored post-CAS state after a cancellation race. R cancellation test. |
| U026 | Open | No evidence-based renew command. Recreate only after reviewing effects; uncertain attempts never auto-renew. |
| U027 | Implemented | Assistant close stops watcher; unresolved known workers retain policy resources instead of silently removing guards. U shutdown probe/test; shutdown is still cooperative. |
| U028 | Implemented | Assistant adapter converts contradictory completed/false to failed; scheduler refuses explicit false renewal. R false-success test. |
| U029 | Partial | Shared atomic tool/model/iteration/replan reservations and elapsed deadlines. R budget tests. Hard memory cap and actual tokenizer accounting not implemented. |
| U030 | Partial | General Python tools still have cooperative deadlines. Workspace subprocesses now install POSIX limits before exec or gate release on a Windows Job Object; H memory/timeout tests. Native Windows focused Job/output/timeout tests now pass in CI; see platform evidence. |
| U031 | Implemented | Initial model inference receives request cancellation token. Updated T integration doubles; cancellable inference queue. |
| U032 | Partial | Thinking calls reserve shared calls and input/output token allowance, including HTTP retries. R prepaid-retry test. Token counts remain estimates, not exact usage. |
| U033 | Implemented | Zero revisions stays zero; configured multi-replan ceiling replaces hardcoded one. Updated replanning tests explicitly set their desired limit. |
| U034 | Implemented | Generated plan nodes advertise zero automatic retries, rather than unused retry behavior. `planner.py`; conservative no-replay policy. |
| U035 | Implemented | Replanner receives actual attempted DAG step indices, including launched siblings. `orchestrator.py`; DAG suite. |
| U036 | Implemented | All collected sibling observations checkpointed and launched siblings finalized before failure return. Unknown outcomes are preserved. DAG suite/source review. |
| U037 | Implemented | Verified branch state requires nonempty independent verified checks, distinct from succeeded. `orchestrator.py`; DAG suite. |
| U038 | Partial | Compound and DAG pools use configured readonly concurrency. No single machine-wide limiter across all delegated/independent processes. |
| U039 | Implemented | Aggregate input bound is checked before compound parsing. R aggregate-length test. |
| U040 | Implemented | Compound parser validation errors return structured invalid responses. `orchestrator.py`; parser/compound suite. |
| U041 | Partial | Context-copied children share parent budget reservations. Unified parent-child trace/cancellation allocation is not complete. |
| U042 | Implemented | Unknown per-request autonomy is rejected before dispatch. R invalid-autonomy test. |
| U043 | Implemented | Context-local authorization is bound to exact tool + arguments. R cross-thread and different-action test. |
| U044 | Implemented | Direct helper checks argument-aware PermissionManager policy. R output-path dynamic-permission test. |
| U045 | Implemented | Atomic INSERT OR IGNORE effect claim refuses existing in-flight/unknown/failed entries; successful effects deduplicate. R concurrent-claim test. |
| U046 | Implemented | Override actions and DAG metadata revalidated/canonicalized. R invalid override test. |
| U047 | Partial | Independent bounded write/copy/move/archive/patch checks added; most other tools still require owner review. H evidence tests. |
| U048 | Implemented | Bounded regular-file byte digests, captured move preimage plus source absence, and binary copy checks. H tampering tests. Missing evidence/directory moves remain unknown rather than verified. |
| U049 | Implemented | Patch apply/revert now re-read live contained files and compare before/after digests, including newly-created-file absence on revert. H patch mutation test. |
| U050 | Open | Objective proof remains narrow; no general typed acceptance engine was introduced. |
| U051 | Implemented | Coverage tuple ordering corrected. T confidence tests, R partial-coverage counts, updated probe. |
| U052 | Partial | Explicit negation/numeric contradictions detected before overlap scoring. No evaluated multilingual semantic contradiction model. |
| U053 | Implemented | Parseable JSON alone gets unknown confidence, not high assurance. T/probe coverage; planner still validates schema. |
| U054 | Partial | Numeric tokens retained and whole-number coverage required. R numeric tests. Units, dates, inequalities, and typed constraint reasoning remain incomplete. |
| U055 | Implemented | Unicode letters/marks/numbers preserved, including Bengali combining marks. R Bengali test. This is token coverage, not semantic understanding. |
| U056 | Partial | Reject revisions that reduce requirement coverage except explicit safety repair. Evidence-based quality comparison remains heuristic. |
| U057 | Partial | Final/fallback thinking output runs action-claim safety gate. T risky-draft tests. Phrase-based detection is not universal factual verification. |
| U058 | Partial | Thinking deadline reaches inference queue/HTTP timeout and UI says cooperative. R queue deadline test. Arbitrary native/model calls are not forcibly terminated. |
| U059 | Implemented | Second opinion no longer receives the candidate to agree with. `reasoning.py`; T stage tests. Correlated-model error remains U061. |
| U060 | Implemented | Configured minimum confidence now causes abstention in thinking output/fallback. R minimum-confidence fallback test; heuristic confidence is not factual proof. |
| U061 | Open | Model roles may still share one model; no diversity or quality improvement claim. |
| U062 | Implemented | Every review target is resolved/contained and read through guarded file I/O. R outside-path test. |
| U063 | Implemented | Manifest reads use guarded containment; escaping symlinks rejected. R manifest-symlink test. |
| U064 | Partial | Workspace runner applies limits before project exec and refuses Windows job failure. H real subprocess tests. Full filesystem/cgroup/native-network isolation is not supplied. |
| U065 | Implemented | Option-shaped pytest targets rejected; validated targets follow --. R option-injection test. |
| U066 | Implemented | Combined stdout/stderr quotas and concurrent bounded drains replace unlimited capture for workspace commands and Git. Timeout/overflow kills and post-kill waits are bounded. H noisy-child and inherited-pipe tests. |
| U067 | Implemented | Resolve/validate entire address answer at connection time; connect numeric addresses without re-resolution. HTTPS preserves original hostname/SNI and certificate verification. H rebinding/mixed-answer/TLS tests. |
| U068 | Implemented | Protected public urllib opener disables ambient proxies. `security/net_guards.py`; network regression suite. |
| U069 | Partial | Playwright context routes same-origin HTTP through pinned fetches; redirects, subresources and forms are mediated, SW/WebSockets/downloads blocked. Selenium refuses unguarded automation. H policy tests; actual Chromium acceptance passes locally and in CI; OS-level native egress containment remains separate. |
| U070 | Open | Python socket guard is not an OS firewall. Native child isolation remains platform-dependent. |
| U071 | Partial | Policy event histories are bounded deques (1000 entries). Lifetime totals/durable retention still need policy. |
| U072 | Partial | New writes use versioned cryptography AES-256-GCM, with no legacy fallback. Legacy reads and backup-first transactional schedule migration tested. External cryptographic review and owner migration of existing data remain pending. |
| U073 | Open | At-rest encryption default remains unchanged; no silent key/data migration. |
| U074 | Open | Existing PIN policy remains compatible; stronger new-PIN policy and OS-backed authentication still needed. |
| U075 | Partial | Git discovery cannot walk beyond approved roots; linked/symlinked metadata rejected; risky Git integrations disabled. Hostile metadata races/internal alternates still need audit. |
| U076 | Implemented | Review walk stops at file/directory caps; reads bounded by configured byte maximum. `dev_assistant.py`; developer regression suite. |
| U077 | Implemented | Result explicitly identifies manifest_inventory and security_scan_performed=false. Not presented as vulnerability-advisory coverage. |
| U078 | Implemented | Submitted flag only follows successful backend click/wait; separate submission_verified=false. `web_automation.py`; actual same-origin form submission acceptance passes; no application receipt is claimed. |
| U079 | Implemented | BEGIN IMMEDIATE serializes sequence/tip read + event insert across writers. R concurrent-recorder test. |
| U080 | Implemented | Versioned canonical v2 hash covers audit-significant metadata; legacy v1 chains readable. R metadata-tamper test. |
| U081 | Open | No externally trusted/signed checkpoint. Arbitrary full database rewrite remains outside the local-chain threat model. |
| U082 | Implemented | Oversized payloads serialize a valid bounded JSON envelope with truncation marker. R JSON test. |
| U083 | Implemented | Task metrics derived from durable run states instead of repeated terminal update increments. Updated probe and observability suite. |
| U084 | Implemented | Removed per-run sequence cache; sequence is transactionally derived from DB. `observability.py`. |
| U085 | Implemented | Clear-all removes reflection rows; DB-backed reflection reads no longer resurrect empty DB from stale cache. Memory suite and updated probe. |
| U086 | Partial | Enabled-vault new requests use AES-GCM. Explicit backup-first migration now upgrades legacy/optional plaintext schedule rows transactionally; H rollback/roundtrip tests. Other SQL fields are not automatically field-encrypted. |
| U087 | Partial | SQLite reflection history capped at 200. Owner-controlled jobs/runs/audit retention with trustworthy checkpoints remains open. |
| U088 | Open | Structured memory retrieval still preselects newest 200; corpus-wide indexed relevance not added. |
| U089 | Partial | Exclusive random mkstemp, descriptor write/fsync and parent recheck fix demonstrated temp-symlink attack. Descriptor-relative parent-race hardening remains. |
| U090 | Implemented | Unique temporary files, local write lock, and atomic no-clobber publication avoid same-process temp collisions. R concurrent-write test. |
| U091 | Implemented | Bound callback default fixes B023; full Ruff gate now passes including new code/tests. |
| U092 | Partial | 747 discovered tests; 742 pass locally with Qt/browser enabled, five explicit skips. All 15 platform CI jobs pass. New coverage percentage and physical-hardware evidence remain unmeasured. |
| U093 | Implemented | Python 3.11–3.14 core jobs pass on Linux and Windows; Windows Qt and Linux offscreen jobs pass. Run 46171800935 at c97e305; see platform report. |
| U094 | Partial | Close now requests cooperative emergency cancellation. No safely bounded forced GUI shutdown for stuck native workers; actual Qt construction and button behavior pass; forced native-worker shutdown remains unbounded. |
| U095 | Implemented | Actual eight-case off/ultra evaluation completed against preloaded Qwen 0.5B: off 6/8, ultra 5/8, no engine/runtime errors. Answers, corpus/model hashes and timings retained. Ultra was worse/slower here; no general quality claim. |
| U096 | Partial | Dev constraints and cryptography 48.0.1 pin supplied. Remaining GUI/speech/model optional-runtime dependencies are not fully locked/tested. |
| U097 | Implemented | All checkout/setup-python uses pinned to exact verified upstream v7 commit SHAs. `.github/workflows/tests.yml`. |
| U098 | Partial | Outer orchestrator requests serialized with a bounded wait, preventing interleaved active histories. No per-conversation IDs or atomic session-setting isolation yet. |
| U099 | Open | Semantic search still loads all vectors and rechecks freshness after inference. No unsafe freshness-cache shortcut introduced. |
| U100 | Implemented | Context overflow refuses inference with actionable error instead of discarding the audit result. `llm_manager.py`; context/agentic suite. |

## Important residual boundaries

1. **Timeout is not termination.** Tool and scheduler supervision stops waiting,
   marks uncertain effects `UNKNOWN_OUTCOME`, and refuses local dispatch while
   the worker is active. Python threads are not forcibly killed. A separate
   process can still run other jobs. Durable claims prevent replay of the same
   saved attempt; they do not constitute machine-wide worker isolation. A crashed
   attempt remains disabled/running for reconciliation. Shutdown retains guards
   and resources if a known unresolved worker remains.
2. **Budgets are not an OS resource sandbox.** Counters are atomically reserved
   before calls. Compound work shares the tracker. Model input tokens are
   estimated and an output allowance is reserved; exact tokenizer accounting and
   global/aggregate supervision of all tools remain incomplete. Workspace commands
   now have pre-exec resource limits; those do not apply to every Python thread. HTTP timeouts are not a universal
   wall-clock kill switch for native code or malicious slow streaming.
3. **Verification stays conservative.** Writes/copies/moves/archives/patches now have bounded digest checks and
   captured evidence where required. Missing evidence never earns `verified`. Many objectives still require owner review.
4. **Containment is improved, not universally race-free.** Unique exclusive file
   temporaries and atomic no-clobber publication remove the demonstrated temporary
   symlink and concurrent-name bugs. Hostile parent-directory replacement still
   needs descriptor-relative OS hardening. Developer tests use the existing sandbox
   backend, whose degraded modes are not a full filesystem/native-network sandbox.
5. **Network and cryptography work remains.** Connect-time pinning and guarded page egress are implemented, and new
   envelopes use AES-GCM with an explicit migration utility. OS firewall isolation,
   external crypto review, encryption defaults and stronger offline authentication
   assurance remain open or partial.
6. **Privacy migrations are not implicit.** New schedule requests use the enabled
   vault, and secret-like plaintext requests are rejected when it is disabled.
   Legacy schedule rows are not automatically re-encrypted; owner-invoked migration
   is now available with a required new snapshot and unchanged existing key. Redaction is heuristic.
   Attempt summaries are redacted but not a sealed authorization artifact. Clearing
   memory removes reflections, but intentionally retained audit/checkpoint data has
   a separate policy.
7. **No intelligence gain is asserted.** More thinking passes, heuristic coverage,
   and blind second opinions do not prove factual improvement. Hardware/model
   benchmarks and multilingual semantic evaluation remain necessary.

## Next implementation priorities

- U069/U070: add OS-level native egress
  enforcement beyond page routing and Python socket guards.
- U029/U030/U064: extend resource supervision to remaining tool threads, add
  aggregate containment, and reconcile unknown effects durably across restarts.
- U072/U073/U074/U086: externally review the new AEAD/migration implementation,
  broaden explicitly integrated encrypted fields, and improve authentication policy.
- U047/U050: extend typed acceptance contracts beyond bounded filesystem tools
  to substantive objectives and domain-specific tools.
- U081/U087: owner-controlled external audit checkpoints and retention/archival that
  preserves integrity semantics; do not silently truncate the chain.
- U018/U026/U088/U099: owner-installed services, explicit reviewed renewal, and
  corpus-wide scalable retrieval without sacrificing freshness checks.
- U092/U094/U095/U096: remeasure coverage, bound GUI exit, broaden model/corpus
  evaluation beyond the completed small-model run, and fully test dependency locks.
