# Ultron / HINATA: 100 current weaknesses

**Date:** 2026-11-27 (UTC)  
**Scope:** working tree on `arena/041b9d6a-ai-automation`, based on `4b46cd16150fd83b6892ced126a23304e3dd117a`, including the uncommitted Ultron/default-startup changes.  
**Purpose:** identify weaknesses, not silently repair them. No production code was changed for this audit. Earlier audit reports describe different trees; their claims of fixes are not proof that this tree is sound.

## Executive assessment

This is a useful local assistant, but **not yet a reliably bounded, unattended general-purpose agent**. Its strongest existing controls include central permission checks, task checkpoints, blocked private HTTP targets, and scheduler attempt claims. Its weakest areas are inconsistent enforcement at alternate entry points, incomplete budget wiring, overstated verification, and persistence/concurrency edges.

**These are 100 weaknesses, not 100 demonstrated exploitable vulnerabilities.** The register separates reproduced behavior, source-backed defects/risks, intentional capability limits, and validation gaps. A private Python helper bypass is not evidence that a remote user or model can invoke that helper. Default-on Ultron was requested by the owner; the findings criticize configuration precedence and visibility, not that request itself.

### Finding totals

| Evidence class | Count |
|---|---:|
| Reproduced behavioral weaknesses (R) | 36 |
| Source-backed defects or conditional risks (S) | 36 |
| Design/capability limitations (L) | 22 |
| Validation/release gaps (V) | 6 |
| **Total** | **100** |

Severity: **27 high, 69 medium, 4 low**. High severity does not imply a remotely exploitable vulnerability.

### Fix first

1. **U089 — predictable temporary-file symlink overwrite:** a synthetic file outside approved roots was modified by `write_text_file` through a pre-created temporary symlink.
2. **U062/U063 — developer-tool path escapes:** code review and dependency-manifest reads can leave approved roots.
3. **U043/U044 — alternate dispatch authorization gaps:** shared authorization depth leaks across threads, and the direct tool helper misses argument-dependent permissions. Reproduced with dispatch mocked, not destructive actions.
4. **U029/U030/U032 — budgets are not end-to-end limits:** tools, plans and thinking do not consistently share enforced counters/deadlines.
5. **U051 — confidence calculation swaps covered/total:** partial coverage can receive a maximum confidence score.
6. **U079 — simultaneous audit writers fork the chain:** two valid writers can trigger a false tamper alarm.
7. **U001/U002 — Ultron startup overrides saved settings:** saved thinking `off` and `SUGGEST_ONLY` are replaced unless the separate Ultron flag is disabled.
8. **U091 — quality gate currently fails:** one Ruff B023 warning in the previously added Ultron test breaks the configured CI quality job.

## Evidence and reproducibility

- Full portable suite: **681 tests, 626 passed, 55 skipped**, run again under coverage.
- Statement coverage: **61%** (16,532 statements, 6,469 missed). This measures this Linux/core environment, not aggregate Windows/GUI CI coverage; subprocess coverage is not automatically collected.
- `mypy hinata`: **success, 87 source files**.
- `ruff check hinata tests scripts`: **one existing B023 failure** at `tests/test_ultron_automation.py:161`. It is a CI blocker, not a demonstrated runtime closure bug: the callback is called synchronously here.
- **36 isolated behavioral probes** accompany this report. They use temporary directories and mocked dispatch/subprocesses where appropriate. No external website, real credential, real destructive tool, or real model is required.
- Probe output: [`audits/ultron-probes.json`](audits/ultron-probes.json).
- Probe source: [`../scripts/audit_ultron_weaknesses.py`](../scripts/audit_ultron_weaknesses.py).

```sh
python scripts/audit_ultron_weaknesses.py
python scripts/run_tests.py
ruff check hinata tests scripts
mypy hinata
python -m coverage run --source=hinata scripts/run_tests.py
python -m coverage report
```

The probe program exits successfully when all documented behaviors reproduce. **That means weaknesses were observed, not that security tests passed.** Some probes use symlinks and were executed on Linux; Windows symlink privileges/native behavior remain untested. Tooling was installed in ignored `.venv`; raw scratch logs are not deliverables.

### Reading the register

- **R** — reproduced behavior; named probe in the evidence column.
- **S** — source-backed defect or hardening risk; relevant path was inspected, but no end-to-end reproduction is claimed.
- **L** — current design/capability limitation, sometimes an intentional safety trade-off.
- **V** — validation, release, or maintainability gap; not proof of a runtime bug.
- **H/M/L severity** — high/medium/low engineering impact; not CVSS. Conditional security risks include their prerequisite.
- Source references are `file:line-range` in this audited working tree. They may move after fixes. Each item is **open** at audit time; recommended fixes are not implemented here.

## A. Default Ultron, settings and control visibility (U001–U012)

| ID | Severity / evidence | Weakness and consequence | Recommended fix / source |
|---|---|---|---|
| U001 | M · R `saved_thinking_overridden` | Default startup replaces saved `thinking.mode = "off"` with `ultra`. A remembered mode is not the effective next-start mode. | Define explicit precedence; preserve owner-selected modes or clearly mark the override. `hinata/core/assistant.py:251-261`; `hinata/main.py:600-640`. |
| U002 | H · R `suggest_only_overridden` | A configuration explicitly selecting `SUGGEST_ONLY` is upgraded to low-risk autonomy by default-on Ultron. Existing privacy/safety expectations can change on upgrade. | Preserve stricter explicit autonomy or require a migration acknowledgement. `hinata/core/assistant.py:251-261`; `hinata/config.py:327-330`. |
| U003 | M · R `preset_drift` | `/ultron on` does nothing if the snapshot already exists. After `/think off`, status can say Ultron is enabled while thinking remains off. | Reapply the preset explicitly or derive an effective/preset-modified state. `hinata/core/assistant.py:251-278`. |
| U004 | M · R `preset_discards_edits` | Turning Ultron off restores the old snapshot, discarding profile/autonomy changes made while it was enabled. | Track which fields the preset still owns; preserve intervening owner edits. `hinata/core/assistant.py:254-267`. |
| U005 | M · L | Desktop users get default-on autonomy without a dedicated Ultron on/off control. The thinking dropdown changes only thinking, not autonomy. | Add a visible preset/autonomy indicator and disable button. `hinata/interface/main_window.py:192-194,285`; `hinata/interface/dialogs.py:85-126`. |
| U006 | L · L | `deliberation_available` checks flags, not model installation or service health. It can be true while Ollama is unusable. | Separate enabled/configured/ready states and link the readiness check. `hinata/core/assistant.py:270-281`. |
| U007 | M · R `no_llm_thinking_status` | With inference disabled, enabling Ultron sets `thinking_status.enabled` true while `ultron_status.deliberation_available` is false. Two status surfaces disagree about effective operation. | Compute effective thinking state consistently using the LLM enable flag. `hinata/core/assistant.py:275-276,292-304`. |
| U008 | M · S | `persist_thinking_mode` resolves environment/default paths, not the CLI `--config` path. `--config custom.toml think deep --remember` can write a different file. | Carry the loaded config path to the persistence function. `hinata/main.py:600-616`. |
| U009 | M · R `persist_single_quotes` | A valid TOML single-quoted `mode = 'balanced'` is not replaced, yet the in-memory config is changed as though the save succeeded. | Use TOML-aware editing and verify the persisted value. `hinata/main.py:628-638`. |
| U010 | M · R `persist_indentation` | A valid indented mode key is not recognized; persistence appends another mode key and creates invalid TOML. | Parse/edit keys rather than relying on beginning-of-line regexes. `hinata/main.py:625-634`. |
| U011 | M · S | Thinking config persistence overwrites the live file directly, without atomic replacement, locking or backup. A crash or concurrent settings edit can corrupt/lose configuration. | Atomic temporary-file write, fsync and guarded replacement. `hinata/main.py:635-636`. |
| U012 | M · L | Ultra is selected on every default startup regardless of available hardware/model capacity. Extra passes can make a low-memory laptop slower without improving correctness. | Benchmark-backed hardware-aware defaults or adaptive fast/deep routing; retain owner override. `hinata/core/assistant.py:258`; `hinata/core/reasoning.py:85-111`. |

## B. Scheduler and unattended operation (U013–U028)

| ID | Severity / evidence | Weakness and consequence | Recommended fix / source |
|---|---|---|---|
| U013 | H · R `schedule_budget` | A job's `budget_json` is stored but not enforced by the scheduled runner. A saved zero-tool budget still dispatches. | Validate job budgets and pass a shared tracker through the execution path. `hinata/tasks/scheduler.py:50,73-86,163`; `hinata/core/assistant.py:284-290`. |
| U014 | M · R `schedule_truncation` | Requests longer than 2,000 characters are silently truncated; trailing constraints can disappear. | Reject oversized requests or require explicit user-approved shortening. `hinata/tasks/scheduler.py:77-78`. |
| U015 | M · R `schedule_expiry_snapshot` | Expiry is checked against one timestamp captured at tick start. A later job can begin after its actual expiry if an earlier job runs slowly. | Re-read wall time immediately before each claim; include expiry in the atomic claim. `hinata/tasks/scheduler.py:114,124,153-158`. |
| U016 | H · S | `runner(row)` has no scheduler-level timeout/cancellation boundary. A blocked runner stalls the watcher and all jobs behind it. | Supervised execution with bounded deadlines and unknown-outcome reconciliation, not blind retries. `hinata/tasks/scheduler.py:162-172`. |
| U017 | M · L | Recurrence is based on tick-start time, not completion time. Long jobs can become overdue immediately and run again on the next poll. | Offer fixed-delay versus fixed-rate scheduling with explicit missed-run/coalescing semantics. `hinata/tasks/scheduler.py:173-174`; `hinata/main.py:520`. |
| U018 | M · L | The watcher is foreground-only and is not automatically installed/resumed as an OS service. Closing the terminal or rebooting stops automation. | Optional owner-installed service/Task Scheduler integration with safe recovery. `hinata/main.py:498-520`; `docs/ULTRON_MODE.md`. |
| U019 | M · L | An online-required job without expiry can remain blocked forever; blocked polls do not consume `max_runs`. The cap limits attempts, not total lifetime. | Separate maximum waiting lifetime/blocked-age limits from attempt caps. `hinata/tasks/scheduler.py:47,130-146`. |
| U020 | L · R `schedule_online_type` | `requires_online` lacks strict boolean validation. Integer 2 is persisted as a flag instead of rejected. | Validate booleans at the scheduler API boundary, not only CLI parsing. `hinata/tasks/scheduler.py:49,84`. |
| U021 | M · R `schedule_integer_overflow` | Interval integers have no upper bound. Values outside SQLite's signed-integer range raise `OverflowError`, which the CLI's outer exception list does not catch. | Validate an operational maximum and finite next-run arithmetic. `hinata/tasks/scheduler.py:58-59,82`; `hinata/main.py:592`. |
| U022 | M · L | Normal listing returns only 50 jobs with no pagination or schedule-ID lookup. Older parked jobs can disappear from the management view. | Add pagination, filters and lookup by ID. `hinata/tasks/scheduler.py:99-104`; `hinata/main.py:521`. |
| U023 | M · L | Only the last status/task ID and cumulative count are retained for a schedule. There is no schedule-attempt history linking every run to its task and outcome. | Add an append-only attempts table with start/end times and run IDs. `hinata/database/migrations.py:294-313`; `hinata/tasks/scheduler.py:180-187`. |
| U024 | M · L | A scheduled single-tool approval request loses its pending action details: the runner returns only status/task ID, and single commands commonly have no task ID. | Persist a reviewable pending command/approval artifact and expose it to the owner. `hinata/core/assistant.py:284-290`; `hinata/core/orchestrator.py:1092-1098`. |
| U025 | L · R `schedule_cancel_report` | Cancellation during execution is retained correctly in the database, but `tick()` still reports the late runner's `completed` result. Returned and durable status disagree. | Check completion update rowcount and return the effective stored state. `hinata/tasks/scheduler.py:181-188`. |
| U026 | M · L | Parked approval/verification/failed jobs have no supported renew/resume action. This is safer than replay, but owners must recreate schedules manually after review. | Explicit evidence-based renewal that never retries uncertain effects automatically. `hinata/tasks/scheduler.py:178-179`; `hinata/main.py:165-179`. |
| U027 | M · R `shutdown_omits_scheduler` | `HinataAssistant.close()` does not call `scheduler.stop_background()`. A separately started background scheduler can outlive assistant shutdown/resource cleanup. | Stop/join the scheduler before closing model, speech and policy resources. `hinata/core/assistant.py:560-575`. |
| U028 | M · R `schedule_false_success` | The scheduled runner discards `AssistantResponse.success`; the scheduler renews solely on the string `completed`. A contradictory adapter response can recur after failure. | Validate success/status consistency and require explicit successful completion. `hinata/core/assistant.py:289-290`; `hinata/tasks/scheduler.py:175-179`. |

## C. Execution budgets, delegation and verification (U029–U050)

| ID | Severity / evidence | Weakness and consequence | Recommended fix / source |
|---|---|---|---|
| U029 | H · R `tool_budget_not_enforced` + S | A zero-tool request budget still runs a tool. `record_tool_call` is not wired into dispatch; `execute_plan` has no shared `BudgetTracker` for total tools/iterations/time/memory. Reported limits are not end-to-end guarantees. | Reserve budgets before each model/tool action and share them through plan/delegation; add actual memory supervision where promised. `hinata/core/orchestrator.py:299,535-554,590-870`; `hinata/core/errors.py:181-262`. |
| U030 | H · S | `PlanNode.timeout_seconds` and the configured per-tool limit are not used to preempt node execution. The agent timeout is checked before, not around/after, `authorized_runner`. | Enforce node deadlines in a supervisor; mark timed-out effects uncertain. `hinata/core/planner.py:121`; `hinata/agents/registry.py:715-724`. |
| U031 | M · S | Initial `_model_reply` does not pass the request cancellation token into `complete`, even though the client supports it. A queued inference may ignore per-request cancellation until later orchestration checks. | Thread the token and remaining deadline into the initial call. `hinata/core/orchestrator.py:1215-1227`; `hinata/core/llm_manager.py:589,636`. |
| U032 | H · R `reasoning_budget` | Thinking increments its local call count but never charges the shared request model-call/token budget. Three thinking calls leave the shared count at zero. | Reserve/record every thinking call in the shared tracker and account actual token usage. `hinata/core/reasoning.py:671-684,937-948`. |
| U033 | M · S | Replanning ignores the configured range: `max_replans=0` becomes at least one revision, while execution clamps the effective revision limit to one even when configured higher. | Preserve zero and enforce one consistent owner-configured ceiling. `hinata/core/assistant.py:137`; `hinata/core/orchestrator.py:838-840`. |
| U034 | M · S | Node `max_retries` is validated/stored but not consumed by execution. Retry metadata can imply behavior the runtime does not provide. | Implement safe, idempotency-aware bounded retries or remove the advertised control. `hinata/core/planner.py:122`; `hinata/tasks/store.py:182-183`; `hinata/core/orchestrator.py:768-781`. |
| U035 | M · S | Replanning receives `plan.steps[:index]` as attempted actions, not the actual DAG execution log. Nontrivial topological order can mislabel attempted/unattempted steps. | Build attempted signatures from durable observations, including all wave siblings. `hinata/core/orchestrator.py:842-846`. |
| U036 | H · S | All parallel wave results are collected, but processing returns on the first failed result. Later sibling observations may never be checkpointed even though those tools ran; the stop message says no later steps started. | Persist every launched result before deciding failure/replanning; distinguish started from unstarted siblings. `hinata/core/orchestrator.py:739-759,783-870`. |
| U037 | M · S | A branch condition ending in `.verified` is satisfied by the same `succeeded` state as `.succeeded`, without independent verification. | Track verified postconditions separately from tool-reported success. `hinata/core/orchestrator.py:716-720`. |
| U038 | M · S | Main execution hardcodes two parallel plan workers and four compound workers rather than consistently using `max_parallel_readonly`. A configured value of one does not govern those paths. | Use one concurrency controller for compound requests, plans and delegated work. `hinata/core/orchestrator.py:269-270,742`; `hinata/agents/registry.py:470,857-867`. |
| U039 | M · S | Compound parsing/dispatch precedes the 4,000-character request check. A split request can avoid the aggregate bound when each child fits. | Validate aggregate input and task count before splitting or spawning. `hinata/core/orchestrator.py:259-305`. |
| U040 | M · S | `parse_multitask` calls `parse` before the orchestrator's validation-error handler. A malformed compound can raise instead of returning the usual structured invalid response. | Put compound parsing inside the same validation/error boundary. `hinata/core/intent_parser.py:619`; `hinata/core/orchestrator.py:260-267,314-320`. |
| U041 | M · S | Compound children each create a fresh request budget and run, with no parent aggregate tracker. Splitting can multiply allowable work and loses unified cost accounting. | Parent cancellation/budget/trace with explicit child allocations. `hinata/core/orchestrator.py:272-299`. |
| U042 | H · R `invalid_autonomy` | An unknown per-request autonomy string is not rejected. A typo can fall through to execution instead of suggest-only/fail-closed behavior. | Validate against `AUTONOMY_MODES` at the public process boundary. `hinata/core/orchestrator.py:258,525-544`. |
| U043 | H · R `authorization_cross_thread` | Authorization depth is one shared integer. A direct helper call in another thread can inherit authorization while a legitimate dispatch is active. This is an in-process trust-boundary weakness, not a proven model-accessible exploit. | Thread/context-local, exact-action authorization tokens; never a shared depth flag. `hinata/core/orchestrator.py:131,1446,1464-1468`. |
| U044 | H · R `direct_dynamic_permission` | The direct `_run_tool` guard checks the tool's static tier, missing dynamic cases such as `write_code(output_path=...)` that require confirmation. Normal `_execute` still checks correctly. | Centralize argument-aware permission evaluation even for internal dispatch seams. `hinata/core/orchestrator.py:1431-1453`; `hinata/core/permissions.py:221-224`. |
| U045 | M · S | Idempotency skips only `SUCCEEDED`; an existing `IN_FLIGHT` effect is overwritten and dispatched again if this lower-level path is entered. Task claiming reduces normal exposure but this is not independently safe. | Atomic effect claiming; reconcile in-flight/unknown effects rather than repeat them. `hinata/core/orchestrator.py:1114-1125`. |
| U046 | M · S | `execute_plan(plan_override=...)` trusts a supplied plan object without revalidation, despite its revalidation docstring. Internal callers must preserve undocumented trust assumptions. | Revalidate/canonicalize overrides or accept only a sealed validated-plan type. `hinata/core/orchestrator.py:602-606`. |
| U047 | M · L | Most tools have no independent postcondition checker; the reviewer returns `unknown`. General automatic verification is not implemented. | Add per-tool evidence contracts and retain explicit unknown states. `hinata/agents/reviewer.py:29-99`. |
| U048 | H · R `reviewer_wrong_content` | Write/copy/move review checks only that a target file exists. Wrong content is reported `verified`, and move source removal is not checked. | Compare expected digests/content and source/destination invariants. `hinata/agents/reviewer.py:46-60`. |
| U049 | M · S | Patch apply/revert verification trusts the database state rather than independently checking current workspace bytes. Later edits or an interrupted persistence sequence can invalidate that evidence. | Verify filesystem digests against the patch's before/after state. `hinata/agents/reviewer.py:74-96,125-134`. |
| U050 | M · L | Objective-level automatic proof handles only narrowly phrased folder-existence requests. Most substantive tasks still need human verification. | Typed acceptance criteria and domain-specific objective verifiers; do not equate tool success with goal success. `hinata/agents/reviewer.py:131-161`. |

## D. Reasoning quality and confidence (U051–U061)

| ID | Severity / evidence | Weakness and consequence | Recommended fix / source |
|---|---|---|---|
| U051 | H · R `confidence_counts` | `_verify` unpacks `(covered, total, missing)` into `(total, covered, missing)`. Partial coverage can score 1.0 and report more covered requirements than total. | Correct tuple ordering; test zero/partial/full coverage and confidence monotonicity. `hinata/core/reasoning.py:331-343,1175-1192`. |
| U052 | M · R `negation_consistency` | Word-set overlap calls “safe” and “not safe” answers agreeing; short negation words are excluded. Contradiction detection is not semantic. | Explicit negation/numeric checks plus evaluated semantic contradiction detection. `hinata/core/reasoning.py:297,346-363`. |
| U053 | M · R `json_plan_confidence` | Any parseable JSON object, including `{}`, gets plan confidence 0.85/high. Downstream planner validation helps, but the confidence label is misleading. | Derive plan confidence from schema/contract validation and evidence, not JSON syntax alone. `hinata/core/reasoning.py:1172-1174`. |
| U054 | M · R `numeric_requirements` | Numeric constraints are discarded by keyword coverage. A wrong year, count, byte limit or quantity may be invisible to verification. | Extract and compare typed numeric/date constraints. `hinata/core/reasoning.py:313-328`. |
| U055 | M · R `non_latin_requirements` | Coverage tokenization is ASCII-only. Bengali/Hindi requests can have zero recognized requirements despite meaningful content. | Unicode-aware tokenization and language-specific acceptance tests. `hinata/core/reasoning.py:297,313-328`. |
| U056 | M · S | Revision acceptance checks format, size and risky phrases, not whether coverage/evidence is better than the draft. A fluent but less relevant revision can replace a useful answer. | Compare draft/revision quality against the same acceptance criteria before replacement. `hinata/core/reasoning.py:835-849,1152-1166`. |
| U057 | M · S | On thinking errors/exhaustion the original draft is returned unchanged, even if it contains an unverified action claim. Review is not a universal truthfulness gate. | Run a final action-claim/evidence gate on every display fallback. `hinata/core/reasoning.py:781-790,822-843`. |
| U058 | H · S | Thinking's wall-clock limit is cooperative between calls, not a deadline passed into the model client. One blocking call/semaphore wait can exceed the advertised “hard” time limit. | Deadline-aware semaphore acquisition, HTTP timeout and cancellation supervision. `hinata/core/reasoning.py:663-680,937-948`; `hinata/core/llm_manager.py:409-428`; `hinata/interface/dialogs.py:110`. |
| U059 | M · L | “Independent” second opinion receives the candidate and is told to agree with its claims. That anchors the answer and reduces independence even with a separate reviewer model. | Blind second pass, then evidence-based comparison; evaluate correlated errors. `hinata/core/reasoning.py:1129-1144`. |
| U060 | M · S | `thinking.require_min_confidence` is accepted/validated in config but never read by execution. Enabling it provides no confidence gate. | Wire an explicit abstain/clarify policy into reply/plan acceptance and test it. `hinata/config.py:303,307`; no runtime references outside config. |
| U061 | M · L | Reviewer/chat roles can resolve to the same local model. More roles/passes do not guarantee independent expertise or higher factual accuracy. | Surface actual model identity per pass; benchmark diversity benefits rather than promise them. `hinata/core/providers.py:293-298`; `hinata/core/llm_manager.py:88-97`. |

## E. Developer tools, browser boundaries and security (U062–U078)

| ID | Severity / evidence | Weakness and consequence | Recommended fix / source |
|---|---|---|---|
| U062 | H · R `code_review_escape` | `code_review(path=...)` accepts an absolute/outside target after validating only the workspace root. It reads the outside Python file and exposes findings/structure. | Resolve every target through approved-root containment and safe file opening. `hinata/automation/dev_assistant.py:160-181`. |
| U063 | H · R `dependency_symlink` | Dependency audit reads a manifest symlink directly. A permitted workspace manifest can point to a file outside approved roots and return its contents as dependency lines. | Safe contained reads for each manifest, not only the workspace directory. `hinata/automation/dev_assistant.py:201-215`. |
| U064 | H · S | `DevAssistant.run_tests` runs project Python/pytest on the host with an environment policy, not the confined workspace sandbox runner. Confirmation does not make arbitrary repository test code safe. | Route this tool through the supervised isolation backend; disclose any degraded isolation. `hinata/automation/dev_assistant.py:220-241`; compare `hinata/automation/workspace.py:271-328`. |
| U065 | M · R `pytest_target_flag` | A target starting with `--` passes path containment but is appended to pytest as an option, not a path. This permits unintended option behavior. | Normalize targets and insert `--`; reject option-shaped targets and define node-ID support. `hinata/automation/dev_assistant.py:223-230`. |
| U066 | M · S | Git/test subprocesses capture all output before slicing it for display. A noisy process/repository can consume large memory despite short returned output. | Bounded streaming/spooled capture with output quotas. `hinata/automation/dev_assistant.py:121-124,232-242`. |
| U067 | H · S | Public URL checks resolve DNS before urllib independently connects. Addresses are not pinned or checked at connect time, leaving a DNS-rebinding check/use gap. No live rebinding exploit was performed. | Pin validated addresses with correct TLS hostname verification and validate redirects at the connection boundary. `hinata/automation/web_search.py:314-334,404-417`. |
| U068 | M · S | The public-web opener inherits environment proxy behavior, unlike the loopback opener. Its apparent direct-public-IP guarantees depend on the external proxy's DNS/routing. | Explicit trusted proxy policy or disable ambient proxies for protected fetches. `hinata/security/net_guards.py:71-83`. |
| U069 | H · S | Native browser navigation follows redirects and loads subresources outside urllib's redirect validator. Initial URL validation does not confine Playwright/Selenium traffic. | Browser request interception/egress isolation covering redirects, subresources and form destinations. `hinata/automation/web_automation.py:838-842,892-896`. |
| U070 | H · L | The process offline guard is Python socket monkeypatching, not an OS network boundary. Native libraries/child processes need separate isolation; environment variables alone are not enforcement. | OS firewall/network namespace or sandbox guarantees, with honest degraded-mode status. `hinata/security/offline_guard.py:199-260`; `hinata/automation/workspace.py:261-269,307-328`. |
| U071 | M · S | Offline guard event arrays append indefinitely; only display is limited to the last ten. A long-running watcher can accumulate unbounded policy-event memory. | Bounded ring buffers with total counters and controlled durable retention. `hinata/security/offline_guard.py:139-140,160-166,194-196`. |
| U072 | H · L | Portable field encryption uses a bespoke HMAC-derived XOR/encrypt-then-MAC construction rather than a vetted AEAD library. No cryptographic break is claimed; assurance and migration risk are higher. | Versioned migration to a reviewed AEAD implementation and independent review. `hinata/security/local_protection.py:33-86`. |
| U073 | H · L | At-rest encryption is off by default. Local conversations, identities and other private data rely on filesystem/account protection unless the owner opts in. | Clear privacy setup and OS-protected keys; offer encrypted storage with migration tests. `hinata/config.py:222-223`; `hinata/core/identity_vault.py:191-260`. |
| U074 | M · L | Four-digit PINs are permitted. PBKDF2 slows guesses but cannot add entropy against an attacker who obtains the verifier; online lockout does not protect offline cracking. | Stronger minimum PIN/passphrase policy and OS-backed authentication where available. `hinata/security/authentication.py:35-46`. |
| U075 | H · S | A contained subdirectory is not necessarily a contained Git repository. Git may discover a parent `.git` outside the approved root, so diff/status/commit scope can exceed the checked directory. | Verify `git rev-parse --show-toplevel` containment and restrict pathspecs/repository discovery. `hinata/automation/dev_assistant.py:65-75,115-148,248-289`. |
| U076 | M · S | Code review's 30-file cap breaks only the filename loop, not `os.walk`; it can keep traversing a huge tree. Reads also load whole files before analysis. | Bound directories, total bytes and wall time; stop the outer traversal at the file cap. `hinata/automation/dev_assistant.py:171-181`. |
| U077 | M · L | `dependency_audit` lists manifest lines; it does not resolve versions or check vulnerability advisories. Its name can be mistaken for an actual dependency security audit. | Rename to manifest inventory or implement a provenance-aware vulnerability/lockfile audit. `hinata/automation/dev_assistant.py:201-218`. |
| U078 | H · S | Playwright and Selenium return `submitted=True` whenever a submit selector exists, even after a caught click/submit failure. This can falsely report a consequential external action. | Record successful click/navigation/server evidence separately; otherwise report unknown/failed submission. `hinata/automation/web_automation.py:868-877,931-937`. |

## F. Persistence, auditing and filesystem integrity (U079–U090)

| ID | Severity / evidence | Weakness and consequence | Recommended fix / source |
|---|---|---|---|
| U079 | H · R `audit_two_writers` | Audit-tip read and event insert use separate transactions and per-instance locks. Concurrent recorders can both use the same predecessor, forking the chain and producing a false tamper alarm. | Serialize tip read+insert in one database write transaction. `hinata/core/observability.py:186-234`. |
| U080 | M · R `audit_metadata_unhashed` | The audit hash excludes model identity, duration, retry count, correlation ID and timestamp. Changing those columns does not break integrity verification. | Canonically hash all audit-significant fields with a versioned schema. `hinata/core/observability.py:16-43,210-231,299-333`. |
| U081 | M · L | The hash chain has no external trusted anchor/signature. A writer able to rewrite the database can recompute the chain or remove its tail without an external witness. | Signed/externally checkpointed tips and explicit threat-model wording. `hinata/core/observability.py:16-43,299-341`. |
| U082 | M · R `audit_json_truncation` | Audit payloads are JSON-encoded and then cut at 4,000 characters, potentially storing invalid JSON. Downstream analysis/export cannot reliably parse them. | Truncate structured fields before serialization and add a truncation marker. `hinata/core/observability.py:185`. |
| U083 | L · R `metrics_double_count` | Every repeated terminal `update_run` increments success/failure counters, even if the run is already terminal. Metrics can show more successes than started runs. | Count state transitions transactionally or derive metrics from stored runs. `hinata/core/observability.py:147-155`. |
| U084 | M · S | Per-run sequence state is retained forever in `_sequences`. Long-lived scheduled sessions accumulate entries even after tasks finish. | Evict terminal-run cache entries; recover sequence lazily from storage. `hinata/core/observability.py:53,81-83,157-166`. |
| U085 | H · R `clear_retains_reflections` | `clear memory` with scope `all` leaves reasoning reflections, including objective/lesson text, in storage. “All” is not all user-derived memory. | Define a complete erasure inventory, including reflections/caches; distinguish legally/operationally retained audit data. `hinata/core/memory.py:506-562`; `hinata/core/reasoning.py:549-560`. |
| U086 | H · R `schedule_unredacted` + S | Scheduler request text is stored verbatim with no redaction or envelope encoding. Envelope encryption elsewhere does not automatically protect this SQL write. | Encrypt sensitive schedule fields and provide redacted display/export views. `hinata/tasks/scheduler.py:69-86`; `hinata/database/database.py:160-167`. |
| U087 | M · S | Jobs, runs, trace events and database-backed reflections have no automatic retention policy. The in-memory reflection cap does not bound the SQLite table. | Owner-configurable retention/archival with audit-chain checkpoint semantics. `hinata/core/reasoning.py:544-560`; `hinata/tasks/scheduler.py:69-89`; `hinata/core/observability.py:85-100,210-235`. |
| U088 | M · L | Structured memory retrieval considers only the newest 200 approved records. Older highly relevant facts become unreachable to that retrieval path as memory grows. | Indexed relevance search across the corpus, not recency-only preselection. `hinata/core/memory.py:413-419`. |
| U089 | H · R `predictable_write_temp` | The write temporary name is predictable from filename/PID and is opened with `write_bytes`, which follows a pre-existing symlink. An attacker/process able to create that entry in an allowed folder can overwrite an outside target. | Exclusive unpredictable temporary creation, no-follow semantics and descriptor-based containment checks. `hinata/automation/file_manager.py:255-258`. |
| U090 | M · S | The same temporary filename is reused by concurrent writes to the same target within one process. Writers can overwrite/remove each other's temporary file or report an unrelated failure. | Unique temporary files plus per-target locking/conflict detection. `hinata/automation/file_manager.py:255-264`; `hinata/interface/main_window.py:173`. |

## G. Validation, delivery and scaling (U091–U100)

| ID | Severity / evidence | Weakness and consequence | Recommended fix / source |
|---|---|---|---|
| U091 | M · V (Ruff reproduced) | The current quality gate fails on B023 in the previously added Ultron test. The full unit suite passing did not detect this release blocker. | Bind the callback argument explicitly and run every configured CI gate before claiming readiness. `tests/test_ultron_automation.py:161`; `.github/workflows/tests.yml:quality`. |
| U092 | M · V | This core-only run covers 61% of statements and skips 55 tests; GUI files are unexecuted here. Passing tests do not validate all deployed surfaces. | Run optional-dependency/Windows suites and collect merged subprocess-aware coverage; prioritize risk paths rather than a cosmetic percentage. Evidence: coverage command above; `.github/workflows/tests.yml`. |
| U093 | M · V | Package metadata declares Python 3.11+ but CI tests only 3.11. New interpreter compatibility is not exercised by the matrix. | Test supported Python versions or narrow the support declaration. `pyproject.toml:requires-python`; `.github/workflows/tests.yml:python-version`. |
| U094 | M · S | The desktop refuses to close while any task is pending. A permanently blocked worker can trap normal window shutdown without a force-close/cancel-and-exit path. | Cooperative cancel with bounded shutdown and an explicit safe forced-exit choice. `hinata/interface/main_window.py:514-520`. |
| U095 | M · V | Portable reasoning tests use fake models; real acceptance is opt-in. There is no demonstrated benchmark showing default ultra improves factual accuracy, task success or latency on the user's model/hardware. | Add a versioned real-model evaluation set and latency/resource baselines before claiming intelligence gains. `tests/test_reasoning_engine.py:1-6`; `tests/test_real_offline_acceptance.py:18-26`; `.github/workflows/tests.yml`. |
| U096 | M · V | Optional runtime/dev dependencies use version ranges without a repository lock/constraints set. Fresh installs can differ from tested environments. | Publish tested constraints/lockfiles and update them through dependency CI. `pyproject.toml:project.optional-dependencies`; `requirements.txt`. |
| U097 | M · V | CI actions use mutable version tags rather than immutable commit SHAs. Build provenance depends on future tag resolution. This is a supply-chain hardening gap, not a claim those actions are compromised. | Pin action SHAs and automate reviewed updates. `.github/workflows/tests.yml:actions/checkout,actions/setup-python`. |
| U098 | M · S | Four concurrent GUI requests share one conversation store and global assistant settings. One request can retrieve another active request's recently appended history; there is no per-conversation/request snapshot isolation. | Session IDs, stable history snapshots and scoped setting changes. `hinata/interface/main_window.py:173,832-836`; `hinata/core/orchestrator.py:313,1217-1222`; `hinata/core/memory.py:50-71`. |
| U099 | M · L | Semantic search loads all matching vector rows and validates source contents twice per query. Integrity is conservative, but large corpora can cause high I/O, memory use and repeated PDF extraction. | Incremental freshness cache with trustworthy invalidation and persistent ANN metadata; preserve freshness guarantees. `hinata/knowledge/index.py:422-469`. |
| U100 | M · S | Context assembly can report overflow without resolving it; `complete` discards the audit result and sends the oversized system/user context anyway. Essential instructions may be truncated downstream. | Enforce token budgets before inference, prioritize constraints and expose unavoidable overflow to the owner. `hinata/core/providers.py:470-515`; `hinata/core/llm_manager.py:614-620`. |

## Suggested remediation sequence

1. **Containment/authorization:** U089, U062–U065, U075, U043–U046. Add regression tests that prove the unsafe path is blocked; do not remove normal approval checks to make tests pass.
2. **Truthfulness and budgets:** U029–U038, U048–U058, U078. Unify budget ownership, honor configuration, persist all launched observations and replace misleading confidence/effect claims.
3. **Default behavior and privacy:** U001–U011, U073, U085–U086. Make the requested default visible without silently overriding stricter saved policies; define complete erasure semantics.
4. **Durable automation:** U013–U028, U079–U084, U087. Keep the existing no-blind-replay safeguard; add controlled review/renewal rather than automatic retry of unknown effects.
5. **Operational evidence:** U091–U100 and remaining capability limits. Run Linux and target-Windows acceptance, real-model quality tests, and dependency/release gates.

## Boundaries of this audit

No live model-quality, physical microphone/speaker/GPU, real browser submission, external SSRF, or target-Windows security test was performed. Source-backed risks such as DNS rebinding, parent-repository discovery and browser egress need dedicated isolated integration tests before claiming an exploit. Existing controls were not removed and no fixes are claimed by this document. Stronger autonomy should wait for the high-priority enforcement and truthfulness issues above.
