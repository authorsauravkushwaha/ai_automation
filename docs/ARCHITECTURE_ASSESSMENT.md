# Architecture assessment and roadmap

## Audit of the existing application

HINATA is a Python 3.11+ Windows-oriented desktop/CLI assistant. `main.py`
and the PySide6 interface share the `HinataAssistant` composition root. Its
Ollama client restricts its endpoint to loopback, ignores proxy settings and
refuses redirects. The intent parser and JSON planner feed the action validator,
permission manager and a fixed tool dispatch; plans contain at most five safe
steps and require user approval. SQLite persists conversations, commands and
preferences. Optional faster-whisper/Vosk, pyttsx3/Piper and audio adapters
provide voice. Setup scripts install dependencies and models during connected
setup. Existing UI, speech, tools and memory have been retained.

The pre-existing implementation is alpha, not a general autonomous agent:
planning is single-shot and stops on a failed step; it originally had no specialist
workers or independent objective verification. Filesystem
checks use path resolution and are not an OS sandbox: a concurrent attacker may
swap symlinks between check and use. Ordinary app launches and local processes
are not network-isolated. Conversation history is automatically persisted up to its
configured retention limit. Normal model requests can download when `auto_pull`
is explicitly enabled, and Ollama itself is a separate process outside app
control. `start.bat` previously ran online repair without a prompt; it now asks
before connected setup/repair. Optional Linux namespace and Windows Sandbox
launchers are described in OFFLINE_ISOLATION.md. They are not system-wide
network blocks or comprehensive connection monitors.

## Increment delivered

`hinata.knowledge.index` adds an explicit, approval-gated text import backed by
SQLite, bounded chunks, exact-word search with source/chunk citations, stale
source exclusion and index removal. Existing validators, permissions, audit and
CLI/GUI command routing are reused. Import does not run file instructions.
Search excerpts are displayed to the user, not replayed into model conversation
history or stored in task-result payloads. The database itself stores the full
indexed text and paths unencrypted. A user-requested clear of all memory deletes
the index, not original files. Individual index removal requires approval.

An optional Ollama embedding adapter stores normalized vectors in SQLite and
ranks current chunks by cosine similarity. `ask knowledge` sends retrieved
passages to the configured local chat model, validates citation labels and
returns a display-only answer. This does not establish factual correctness or
robust prompt-injection resistance. TXT and configured text formats, DOCX main
body and optionally PDF text are supported. An approval-gated bounded folder
sync handles updates and missing entries, but is not automatic. PDF OCR and
page-level citation are not implemented. Metadata-based stale checks can miss edits with deliberately preserved size and timestamp. Removal
requires a path still inside a configured root. Re-import explicitly after edits;
if roots change, clear all memory to remove otherwise inaccessible indexed copies.
Index size grows with explicit imports; this implementation is intended for
small libraries, and search scans stored chunks. No claim is made that retrieved
content is trustworthy or that prompt-injection defenses are comprehensive.

## Durable plan checkpoints

Approved-plan proposals and per-step states are stored locally. The CLI can list
and inspect them or cancel a proposal still awaiting approval. Interactive approval
is bound to the exact displayed steps and source, and an atomic claim prevents
reusing the same task ID. A RUNNING step left after a crash is explicitly uncertain;
no automatic action replay or automatic task resumption occurs. After successful
steps, a separate read-only reviewer checks created folders and explicitly marks
other postconditions unknown. A single-step exact `ensure folder PATH exists`
request, or an exact comma-separated `ensure folders PATH1, PATH2 exist` request
with two to five unique paths and matching create steps, can be verified against
the filesystem with recorded evidence. Other objectives remain VERIFYING until a person records observed
evidence; human attestation is not automatic proof of the objective.
A proposal-only worker can suggest one bounded revision after a failure, requiring
fresh approval. The current runtime now delegates every validated plan node through
registered manifests; model-backed research/coding/reviewer roles first return a
strict report through `AgentExecutor`, while deterministic monitor/execution workers
use the same registry without fabricating model work. No specialist can authorize
its own side effects. Python code inspection uses a bounded AST parser without
executing code; its warnings are heuristic and not a security audit. Iterative
specialist repair, full lifecycle scheduling, automatic action recovery, and a
complete task dashboard are not implemented. Checkpoints are unencrypted and may
contain private paths.

## Agentic OS runtime increment (v1–v6 schema & phases 1–8)

The platform now includes an offline-first agentic runtime built with zero required
third-party runtime dependencies on the core path:

1. **Versioned SQLite migrations (`v1`–`v6`)** (`hinata/database/migrations.py`):
   Ordered schema migrations adding structured 5-tier `memory_records` &
   `memory_revisions`, `tool_idempotency` ledger, `trace_events` observability,
   `scheduled_jobs`, `coding_workspaces` & `coding_patches`, `offline_assets`
   SHA-256 inventory, `knowledge_reembed_queue`, and DAG/lease columns on
   `agent_tasks` and `agent_task_steps`.
2. **Offline policy guard & SHA-256 asset manager** (`hinata/security/offline_guard.py`,
   `hinata/core/assets.py`): Process-level socket interception blocking non-loopback
   egress in `strict_offline` / `offline_default` modes, domain-allowlisted
   `consented_online` mode, offline child-process env sanitization, and traversal-safe
   `.tar.gz` asset bundle import/export with SHA-256 digest verification.
3. **Provider runtime, hardware profiler & role-based model routing**
   (`hinata/core/providers.py`, `hinata/core/llm_manager.py`): `ModelProvider`
   protocol, streaming NDJSON generation, `HardwareCapabilityService` tier sizing,
   `ModelRouter` (`planner_model`, `coding_model`, `reviewer_model`, `strict_model_pin`),
   `CircuitBreaker`, `BudgetTracker`, request-scoped `CancellationRegistry`, and
   `ContextManager` token-budget compaction.
4. **DAG planner, 5 autonomy modes & bounded offline scheduler**
   (`hinata/core/planner.py`, `hinata/tasks/store.py`, `hinata/tasks/scheduler.py`):
   `ExecutionPlan` DAG validation (cycle & write-conflict rejection, parallel
   read-only `topological_batches`, canonical `plan_hash`), five configured autonomy
   modes (`SUGGEST_ONLY`, `APPROVAL_EACH_STEP`, `APPROVED_PLAN`,
   `LOW_RISK_AUTONOMY`, `OFFLINE_AUTONOMY`), explicit lifecycle vocabulary,
   safe `pause_task` / `resume_paused_task`, non-replaying `reconcile_interrupted_task`,
   and `TaskScheduler` offline background jobs.
5. **10-specialist multi-agent coordination & coding workspaces**
   (`hinata/agents/registry.py`, `hinata/agents/runtime.py`, `hinata/automation/workspace.py`):
   Typed `AgentManifest`/`AgentMessage` envelopes, strict model-backed `SubtaskReport`
   preflight, manifest/risk/delegation/concurrency/lock enforcement, durable
   specialist reports, independent reviewer consensus, atomic unified-diff/file
   patch apply & rollback, and `IsolatedSandboxRunner` (Linux `unshare -rn` when
   available + startup Python sitecustomize socket block, `python -I -B`, stripped env,
   CPU/memory `RLIMIT`s).
6. **5-tier structured memory & hybrid structure-aware RAG**
   (`hinata/core/memory.py`, `hinata/knowledge/`): Working/episodic/semantic/
   procedural/archival memory with contradiction supersession, provenance, secret
   blocking, and optional `LocalVault` authenticated envelope encryption; CSV/JSON
   ingestion, Markdown/heading section chunking, `ThrottledKnowledgeWatcher`, hybrid
   exact + lexical + vector scoring, and `verify_claim_citations`.
7. **Desktop Agent Control Center, voice barge-in & honest vision gating**
   (`hinata/interface/control_center.py`, `hinata/speech/multimodal.py`): 6-tab
   PySide6 `AgentControlCenterDialog` accessible from `MainWindow`, `VoiceDiagnostics`
   barge-in TTS interruption, and `LocalVisionAdapter` failing closed unless local
   Ollama vision models or local `pytesseract` OCR are installed.

## 2026-11-15 implementation audit and Phase 0/1 repairs

The audited base was commit `ea3eb9b505df742b382010a67dfa98b5116ad068`. The
previous text above remains a historical assessment of the earlier increment; the
current capability matrix and open work are maintained in
[JARVIS_GAP_REGISTER.md](JARVIS_GAP_REGISTER.md).

This branch now adds a real, bounded specialist runtime. A validated plan node is
assigned to the registered manifest, receives only its declared assignment
context, and (for model-backed roles) must first produce a strict JSON report via
`hinata/agents/runtime.py`. The report is untrusted and cannot execute a tool.
`AgentRegistry` enforces the manifest tool allowlist, risk ceiling, delegation
cycle/depth, concurrency, timeout, locks, and tool budget. The central
`Orchestrator` remains the only side-effect callback and runs the normal
`PermissionManager` and `WorkerCoordinator` path. Assignment messages and final
reports are persisted in SQLite, including model identity, evidence, usage, and
limitations. Deterministic monitor/execution agents use the same delegation and
report path without inventing a model result.

`ObjectiveContract` now distinguishes the request, desired outcome, acceptance
criteria, constraints, exclusions, approved workspaces/data sources, budgets,
risk, approval and verification strategy, and confirmed/inferred/observed facts.
The contract is persisted with a task plan. This is a conservative first-pass
understanding layer; it is not a replacement for owner clarification when a
request is materially ambiguous.

Project creation no longer returns success merely because files were written.
`ProjectArchitectEngine.build_project` records a lifecycle status and check-level
evidence. A passing non-empty test suite can produce `SUCCESS` for a local
model-generated project. Missing/skipped verification produces `PARTIAL`, test
failure produces `FAILED`, unavailable verification produces `BLOCKED`, and a
write error with uncertain effect produces `UNKNOWN_OUTCOME`. The deterministic
offline scaffold deliberately remains `PARTIAL` even when its own smoke tests
pass. Project status and verification evidence are persisted in the versioned
`project_records` and `project_iterations` tables. This is still an initial
bounded scaffold path, not the requested iterative software factory; that gap is
explicit in the register.

## Remaining target-hardware validation roadmap

1. Run physical Windows 11 laptop validation for hidden `ollama serve`, Windows
   Sandbox `.wsb` boot, SAPI/Piper audio playback, microphone capture, and speaker
   thresholds under a real 4 GB–8 GB RAM budget.
2. Perform external cryptographic and OS-level security review (note: `LocalVault`
   now writes AES-GCM v2 via the secure-storage extra and reads legacy v1; Windows
   DPAPI / POSIX `0600` key protection remains. Envelopes are not full database
   encryption; see `ULTRON_HARDENING.md` for migration and pending validation).
