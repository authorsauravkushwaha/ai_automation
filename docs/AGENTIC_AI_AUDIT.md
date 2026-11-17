# HINATA agentic-AI audit and repair register

Audit snapshot: current working tree on `arena/250bdccf-ai-automation`, based on `4b39bc8`.
The audit reviewed the assistant request path, model client/reply parsing, planner and task store,
tool/policy registries, web search, application control, code generation, installation, voice,
GUI concurrency, and the portable test suite. The repository is an agentic-assistant application
using an installed local LLM; it is **not** a foundation-model training project.

## Findings and disposition

### Repaired in this working tree

1. **No live search tool existed.** Added concurrent public-source search across DuckDuckGo,
   Wikipedia, GitHub, and Open Library, with relevant results, source URLs, excerpts, and citations.
   Retrieved web text is explicitly treated as untrusted input, never as policy or tool instructions.
2. **A factual question could reach the LLM without internet grounding.** Common factual/current
   prompts such as “Who is…?”, “Tell me about…”, “latest news…”, and “current price…” now search first.
3. **A search with zero network results failed without recovery.** It now reports an explicit offline
   fallback and uses the local model/indexed files when available; it does not label that answer live-verified.
4. **A search answer could imply a live answer without live sources.** Offline status is included in
   tool data and clearly prefixed in the user-visible response.
5. **Webpage fetching had no SSRF boundary.** Fetch validates HTTP(S), fails closed on unresolved hosts,
   rejects private, loopback, link-local, reserved and metadata-service IP targets, rejects credentials, and validates redirects.
6. **One malformed small-model reply could abort a normal conversation.** Readable topic/fact JSON
   and truncated chat text can be recovered as display-only chat; action-like or blank objects remain rejected.
7. **The global tool catalog inflated each model prompt.** Completion now filters tool schemas by the request.
8. **The prompt imposed an 80-word answer limit.** Removed it; chat and researched replies can be complete.
9. **Completion token limits were too small for research and code.** Budgets now vary by task; code
   generation requests at least 1536 prediction tokens (subject to model/runtime limits).
10. **Ollama models were unloaded after ordinary `complete()` calls.** Completions now pass the
    configured keep-alive value; direct `chat()` retains its legacy zero default for compatibility.
11. **HINATA had no configurable owner name.** Names are persisted in local settings, available through
    chat/CLI/GUI, injected into the assistant instruction, and used in greetings.
12. **Setup did not ask for a name.** Interactive Windows/Linux setup now asks; redirected setup skips
    the question safely and explains how to set it later.
13. **The default TTS voice could be the first male SAPI voice.** Soft-female selection is now the default,
    with a selected rate/volume and a neural option when separately configured/available.
14. **Application launching was effectively static.** Added cached discovery for configured apps,
    Start Menu/Desktop shortcuts, Windows App Paths/Uninstall registry records, built-ins, and Start apps;
    launch paths avoid `shell=True` and uninstall shortcuts are filtered.
15. **Code authoring was effectively Python-only and short.** Added `PolyglotCoder` for 32 named
    languages, optional confined output, Python syntax checking, and offline starters. `CodeInspector`
    remains deliberately `.py`-only.
16. **GUI input was locked during active tasks.** It now remains available and its thread pool can run
    up to four UI jobs; local Ollama inference remains serialized to protect memory.
17. **Compound independent commands had no parallel path.** Clearly segmented, supported read-only
    tasks can execute concurrently; mutating/ambiguous compounds are not auto-split.
18. **Web/installed-app/code/name tools could be missing authorization registrations.** Added their
    schemas, manifests, permissions, planner allowlist, worker and agent registrations.
19. **Model streaming did not share the keep-alive optimization.** Streaming now respects configured
    keep-alive as well.
20. **Web availability/configuration was not visible in status.** `hinata status` now reports whether
    web search is enabled, strict-offline state, and online-first/offline-fallback behavior.
21. **Voice tests and welcome messages were generic.** CLI/GUI welcome and voice-test phrases now use
    the saved owner name.
22. **Documentation implied there was no live-search/offline fallback feature.** README and config
    comments now explain the online-first policy and local fallback.
23. **Regression coverage lacked these paths.** Added tests for query extraction, reply salvage, SSRF
    blocks, deterministic search parsing, offline fallback, shortcut filtering, and polyglot output.
24. **One malformed plan could still expose a JSON-plan failure to a normal question.** A malformed
    structured reply now triggers one safe, text-only local retry; no tool/action is executed on that path.
25. **A syntax error in generated Python was discarded instead of repaired.** The code writer now
    requests one full-source repair for invalid Python/JSON before using an offline starter.

## Minimum agentic standard: current evidence

| Requirement | Current state | Evidence / remaining boundary |
|---|---|---|
| Goal + observable criteria | Partial | `Planner` builds objective contracts; ambiguity and criteria extraction remain heuristic. |
| Validated multi-step plans | Tested | Typed DAG validation, dependencies, write-conflict checks, and previews are covered by planner/agentic tests. |
| Typed, permission-checked tool use | Tested | `ActionValidator`, central `PermissionManager`, manifests, and worker routing gate tools. |
| Observe real outcomes | Tested in supported paths | Tool results/checkpoints are stored; some adapters still rely on tool-reported success. |
| Replan after failure | Partial | Failed plans may produce a separately approved revised plan; no revised work auto-runs. |
| Persist/recover safely | Tested with conservative boundaries | Checkpoints and `UNKNOWN_OUTCOME` prevent blind replay; restart scheduler resume remains open. |
| Verify original objective | Partial | Selected postconditions are independently checked; general tasks may remain awaiting human verification. |
| Respect authority/budgets | Tested in repository | Central permission checks, confirmation/PIN flows, budgets, cancellation, and audit traces exist; OS isolation still needs Windows validation. |

### Remaining limitations (not honestly solvable or verifiable as code-only changes here)

26. **No base/foundation LLM is trained or bundled.** HINATA calls an installed Ollama model. Training
    a competitive general LLM needs very large data, compute, licensing work, and evaluation; this repo
    only supports local inference, model selection, and optional model pulls.
27. **Search providers can rate-limit, block, change markup, or go offline.** Multiple sources and
    graceful fallback reduce the impact but do not guarantee live results.
28. **Offline model answers are not live facts.** They may be useful background, but current events,
    prices, and recent biographies cannot be verified without sources.
29. **Web search reads public HTTP(S) text only.** It does not execute JavaScript, sign in, solve CAPTCHAs,
    or access private/intranet sites.
30. **Full answer quality depends on installed model quality/context.** A 0.5B model can produce weak
    research synthesis; citations and excerpts are supplied, but human review remains appropriate.
31. **Model inference concurrency is one request at a time.** This is a deliberate RAM/VRAM guard for
    small local models. Network retrieval and safe tool tasks can overlap.
32. **Polyglot output is generation, not a compiler suite.** Python syntax is checked; non-Python
    languages need their own locally installed compiler/linter and do not receive universal AST checks.
33. **Offline code starters are templates, not full task-specific generated solutions.** Full custom
    source requires a suitable local coding model.
34. **The project factory is not yet an autonomous multi-day software company.** The repo has bounded
    project scaffolding, workspace patches, tests, and verification, but not a fully iterative
    requirements-to-release loop for arbitrary projects.
35. **Task recovery is intentionally conservative.** Interrupted non-idempotent side effects become
    `UNKNOWN_OUTCOME`; the assistant does not blindly replay them. Restart-driven orchestration and
    automatic compensation are incomplete.
36. **Agent reports do not grant direct authority.** Tool actions still pass centralized authorization;
    sensitive or mutating work may need owner confirmation/PIN.
37. **Windows app discovery/launch is not physically validated in this Linux sandbox.** Registry,
    COM/shortcut and UWP launch behavior needs target-Windows acceptance testing.
38. **Voice selection is best-effort across Windows voice inventories.** Available SAPI voices vary by
    installed language/Windows build; no particular voice is bundled or guaranteed.
39. **The Python socket guard is not an operating-system firewall.** Native child processes and Windows
    firewall boundaries need OS-level testing; strict offline remains a defense-in-depth feature.
40. **No physical microphone, speaker, GPU, or Windows UI acceptance run occurred.** Portable tests
    validate code contracts, not hardware quality, latency, or memory fit.
41. **The current test suite uses stubs/mocks for real model and OS integrations.** Passing tests do not
    substitute for a live Ollama + internet + Windows acceptance run.
42. **Application opening is not arbitrary command execution.** It is intentionally limited to discovered
    or configured installed applications to avoid model-generated shell commands.

## Verification

`python3 scripts/run_tests.py`: **250 passed, 13 skipped** at the time this register was written.
The skipped tests require optional dependencies or native Windows tools. No performance claim,
foundational-model training claim, Windows validation, or independent security audit is implied.
