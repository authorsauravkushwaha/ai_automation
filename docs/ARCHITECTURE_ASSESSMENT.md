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
fresh approval. Deterministic research, code-inspection, memory, and execution workers route
already-authorized actions under separate allowlists; they are not autonomous LLM
agents. Python code inspection uses a bounded AST parser without executing code;
its warnings are heuristic and not a security audit. Plan timeouts, richer specialist scheduling, automatic action recovery
and a task dashboard are not implemented. Checkpoints are unencrypted and may
contain private paths.

## Remaining implementation roadmap

1. Validate and strengthen optional OS isolation on target platforms, including
   IPC, spawned applications, attempted-connection diagnostics, explicit online
   grants, and a real laptop-level disconnected acceptance test. Remove implicit
   downloads from normal operation and validate all launch paths.
2. Add a provider protocol, real streaming, capability checks and controlled
   local-model switching. Test with installed Ollama and cached models.
3. Extend the bounded plan checkpoints and one-shot revision with recovery and
   automatic objective-level evidence-based verification. Never replay an
   interrupted side effect automatically. Expand the limited specialist workers
   with controlled scheduling, coding capabilities and meaningful delegation.
4. Introduce hardened tool isolation, schemas, approvals, audit and subprocess
   limits, followed by adversarial path-race and prompt-injection testing.
5. Validate real-model semantic accuracy and retrieval-grounded answers; add
   approved semantic and episodic memory, PDF OCR/page citations, scalable
   vector indexing and automatic synchronization.
6. Add model/voice/task/approval/knowledge controls to the UI, hardware-aware
   presets and local voice language/asset management; test actual target devices.
7. Pin and audit dependencies, build reproducible releases, perform independent
   security review and complete all acceptance tests in the requested offline
   scenario. The existing mocked suite does not substitute for those tests.
