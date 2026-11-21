# HINATA

**Your files, your models, your machine.** An offline-first Windows desktop
assistant for local conversation, document search, memory and permission-aware
automation.

[![Tests](https://github.com/authorsauravkushwaha/ai_automation/actions/workflows/tests.yml/badge.svg?branch=main&event=push)](https://github.com/authorsauravkushwaha/ai_automation/actions/workflows/tests.yml?query=branch%3Amain+event%3Apush)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[Get started](#one-click-start-from-the-downloaded-zip) ·
[Try without a model](#try-the-core-without-a-model) ·
[Examples](#what-can-i-try) ·
[Security & offline isolation](#security-and-project-status) ·
[Contribute](CONTRIBUTING.md)

> **Alpha software.** HINATA targets Windows 11 and a low-memory laptop; it has
> not been independently security-audited or fully validated on physical devices.
> Check the [roadmap and validation gaps](docs/STAGE_STATUS.md) before relying on it.

## What can I try?

| You want to… | Try this in HINATA | What to know |
|---|---|---|
| Search the live internet | `Search about author Saurav Kushwaha` or `google ...` | Internet is tried automatically when available. If unavailable, HINATA falls back to local models/indexed files and marks the answer as not live-verified. `strict_offline = true` or `web_search_enabled = false` disables web access. No remote AI model is used. |
| Read a public webpage | `fetch webpage https://example.com` | Fetch is restricted to public HTTP(S) pages and blocks private/loopback/link-local addresses. |
| Open an installed application | `open Spotify` or `list installed apps` | Searches configured apps, Start Menu/Desktop shortcuts, Windows registry, and packaged Start apps. |
| Write code in a language | `write code in Rust: implement ...` | Supports 30+ languages; use `output_path` in a normal request to save inside permitted roots. |
| Remember your name | `My name is Saurav` or `our names are Saurav and Priya` | Stored locally; installation asks interactively when a terminal is available. Settings can change it later. |
| See your machine's status | `system info` | Deterministic; no model needed. |
| Search local notes | `index document Documents/notes.txt` then `search knowledge for project` | Indexing requires approval. Files must be under an allowed root. |
| Remember a preference | `remember that my editor is vim` then `list preferences` | Saving requires approval; memory is stored locally in unencrypted SQLite. |
| Inspect Python code | `inspect code Documents/script.py` | Read-only static inspection, not a security audit. |
| Use a local model | Ask a question in the desktop or CLI | Requires an installed Ollama runtime and prepared model. |

HINATA also offers optional semantic retrieval, offline speech, bounded plans,
model-backed specialist preflight, and Windows automation. Specialist reports are
strictly scoped and all side effects still pass through the central permission
manager; this is not an uncontrolled autonomous system. Some components require
separately installed models or hardware. See the [feature details](#local-document-search-and-question-answering),
[capability matrix and gap register](docs/JARVIS_GAP_REGISTER.md), and
[roadmap](docs/STAGE_STATUS.md).

## Try the core without a model

With Python 3.11+ installed, run this from the extracted or cloned project folder:

```text
python -m hinata --no-llm ask "system info"
```

This deterministic command needs no model or paid API. It creates local application
data in HINATA's configured data directory. For the desktop, speech and model-backed
features, follow the Windows setup below.

## Security and project status

- Normal use is **local-first, not an operating-system network boundary**. The
  process guard and coding sandbox are defense-in-depth; the optional [offline
  isolation guide](docs/OFFLINE_ISOLATION.md) describes stronger restrictions and
  their limitations.
- Actions are allowlisted and sensitive actions require approval; arbitrary
  model-generated shell commands are not executed. These controls are not a full
  operating-system sandbox. [Security model](docs/SECURITY.md).
- Project files are not proof of completion. Build results explicitly distinguish
  `SUCCESS`, `PARTIAL`, `FAILED`, `BLOCKED`, and `UNKNOWN_OUTCOME`; missing or
  skipped checks never pass. The deterministic offline scaffold remains `PARTIAL`.
- No model weights or voice recordings are bundled. Connected setup and optional
  downloads require consent; physical audio, Windows isolation, and real-model
  acceptance need testing on the target computer.

## One-click start from the downloaded ZIP

No terminal experience needed:

1. On the GitHub page, click the green **Code** button, then **Download ZIP**. (Or use this link: [Download the repository ZIP](https://github.com/authorsauravkushwaha/ai_automation/archive/refs/heads/main.zip).)
2. Right-click the downloaded ZIP → **Extract All…** and open the extracted `ai_automation-main` folder.
3. Double-click **`start.bat`**.

The start menu guides you through these choices:

1. **Open HINATA normally** after setup. Optional connected component repair is
   separately confirmed before it runs.
2. **Connected setup or full repair**. Approve the download prompt to install
   dependencies, the chat model and speech assets, run tests and open the desktop.
   If setup was interrupted, choose this option again. It needs internet and can
   take several minutes; keep the window open. Semantic search additionally needs
   a separately downloaded embedding model; option 4 explains how to prepare it.
3. **Check this PC and local model** for diagnostics after installation.
4. **Offline readiness and acceptance guide** for preparation, manual checks after
   restart, and the separate Windows Sandbox isolation instructions.
5. **Exit** without setup.

A normal launch is local-first, **not network-isolated**. Declining repair does
not block internet access, and user-modified settings such as `auto_pull = true`
may permit downloads. For an actual network restriction, follow the Windows
Sandbox instructions in the guide; that path and physical voice need testing on
your own Windows computer. Diagnostics do not certify end-to-end readiness.

### Ollama connects automatically

You never have to run `ollama serve` yourself. HINATA connects to the loopback
Ollama runtime on its own:

- `setup.ps1` finds `ollama.exe` even when the freshly installed PATH is not
  refreshed yet (it also checks `%LOCALAPPDATA%\Programs\Ollama`, `%ProgramFiles%\Ollama`),
  starts it hidden on `127.0.0.1:11434`, and pulls the model only when it is missing.
- The desktop window connects as soon as it opens. The badge in the header shows
  **LOCAL ONLY** when the runtime and model are ready, otherwise **OLLAMA OFFLINE**,
  **MODEL MISSING**, or **OLLAMA NOT INSTALLED**. The **Reconnect** button retries, and
  a missing model offers a one-click download.
- If the service is stopped while HINATA is running (reboot, closed terminal, sleep),
  the next message starts it again before inference. If the runtime is genuinely
  unavailable, chat reports *why* and structured commands keep working.
- `hinata doctor`, `hinata model status`, and the in-app status dialog now report the
  connection state instead of a bare "unavailable".

Leave `ollama.model = ""` in `config.toml` to use whichever local model is already
installed; if the configured model is missing but another local model exists, HINATA
uses that one instead of failing. Set `ollama.auto_pull = true` to let HINATA download a
missing configured model once. Set `ollama.auto_start = false` to keep the old
"never start a service" behaviour.

If Windows shows a warning because the file came from the internet, choose **Run anyway** / **More info → Run anyway**. `start.bat` is a plain text script; you can read it before running it.

## Run directly from GitHub — no ZIP needed

### Prerequisites

- Windows 11
- 64-bit [Python 3.11+](https://www.python.org/downloads/windows/) (3.11 recommended)
- [Git](https://git-scm.com/download/win)
- Internet during setup only
- At least 3 GB free disk space for the recommended Python/model downloads

Open **Command Prompt** or **PowerShell**:

```bat
git clone https://github.com/authorsauravkushwaha/ai_automation.git
cd ai_automation
.\setup.cmd -Run
```

That command creates `.venv`, installs HINATA in editable mode, copies `config.example.toml` to `config.toml`, installs/checks Ollama, pulls the explicitly quantized **Qwen2.5 0.5B Instruct Q4_K_M** model, prepares tiny INT8 faster-whisper, runs the test suite and a real text smoke test, then opens the desktop UI.

If Python is missing, install it first:

```bat
winget install -e --id Python.Python.3.11
```

Close and reopen the terminal after installing Python. Ollama is installed through `winget` by the setup script when needed.

If `git clone` says the destination already exists, it has **not** updated that directory. To preserve your existing files, clone into a different directory (choose a name that does not already exist):

```powershell
cd $HOME  # or the parent directory where you want the new clone
git clone https://github.com/authorsauravkushwaha/ai_automation.git ai_automation_clean
cd ai_automation_clean
.\setup.cmd -Run
```

Parser errors such as `Unexpected token '\Scripts\python.exe"'` or `The string is missing the terminator` can result from an older or locally modified checkout. The current bootstrap scripts are ASCII-only so Windows PowerShell 5.1 (`powershell.exe`, which `setup.cmd` starts) decodes them reliably. The clean clone above avoids overwriting local work or configuration. Alternatively, review `git status` in the existing checkout and update it with `git pull --ff-only` if your changes and branch allow it.

To check that the actual launcher parses the script without installing anything or starting the app, run `.\setup.cmd -CheckSyntax`; it should print `Windows setup script syntax OK.`

### After setup

```bat
.\hinata.cmd gui                 :: desktop interface
.\hinata.cmd                     :: interactive terminal
.\hinata.cmd ask "system info"   :: one typed request
.\hinata.cmd listen              :: one 5-second voice request
.\hinata.cmd --no-speak ask "system info" :: mute this request
.\hinata.cmd doctor              :: local diagnostics
.\hinata.cmd status              :: JSON feature/model status
```

Set a PIN before sensitive operations or voice enrollment:

```bat
.\hinata.cmd setup-pin
```

> Setup downloads packages and models. Once those downloads finish, normal chat, speech recognition, speech output, memory, and automation do not require internet. Structured commands also work when Ollama is stopped.

### Hands-free voice, browser search, and scrolling

- **Greeting:** HINATA calls you **Darling** by default. A name you chose during setup or in Settings takes precedence.
- **Voice:** In the desktop app, click **Start hands-free voice** once. Speak a request and pause for about a second. Under **Settings → Voice**, you can change the pause to 0.75–2.5 seconds or tune mic sensitivity for a quiet voice or noisy room. HINATA transcribes it locally, handles the request, and starts listening again after the task and spoken reply finish. Click or say **Pause voice** to stop listening. **Emergency stop** also requests cancellation of active work. The mic is off on launch and this session ends when you close the app.
- **Search in chat:** Say or type `search for train times` to look up public web results in HINATA. If sources are unavailable, HINATA labels the fallback and offers **Try in browser**. Nothing opens until you click.
- **Search in your browser:** Try `search in browser for train times` for your system default, `search in Firefox for train times` for a named Windows-registered browser, or `google train times in Chrome` to use Google in Chrome. The results open in the browser rather than in chat, and Ollama is not required. Search sends the query to a public search provider; both search routes respect the web-search setting and strict offline mode.
- **Scroll:** Put the Windows app or browser in the foreground, leave the pointer over the area you want to scroll, and say `scroll down`, `scroll up`, or `scroll down two notches`. HINATA won't send the scroll to an app in the background. The target app must support mouse-wheel scrolling in that area. To read HINATA’s own chat, say or type `scroll chat up` or `scroll chat down`. New replies leave you at your current reading position. Click **Latest message** or say `go to the latest message` to return to the bottom.

## Local document search and question answering

For lexical search without an embedding model, import a text file within an
allowed root, approve the request, and search:

```text
index document Documents/notes.txt
search knowledge for project
remove document from index Documents/notes.txt
sync knowledge folder Documents
list knowledge
```

For semantic search, first prepare both Ollama and the configured local chat and
embedding models **while online**. For the default embedding model, explicitly run
`ollama pull nomic-embed-text`; `ollama.embedding_model` may instead name another
installed Ollama embedding model such as `bge-m3` (check its hardware needs and
license). Normal semantic indexing never pulls a model. Once prepared:

```text
semantic index document Documents/notes.txt
semantic search for project delivery date
ask knowledge when is the project delivery date?
```

Approve each semantic import. Embeddings are generated locally and saved in
SQLite with the exact embedding-model digest; a changed model requires reindexing.
The model-generated answer is display-only, includes source references, and may
be incorrect or follow malicious text despite instructions to ignore it. Check
the cited passages. These commands do not run code or actions proposed in a
passage or answer. If an embedding model or Ollama is unavailable, the request
fails rather than downloading or silently falling back. `--no-llm` disables this
path; lexical search remains available.

The index accepts configured text extensions, DOCX, and text-based PDF. PDF support
requires the optional pinned dependency, installed during connected setup:
`python -m pip install -e ".[documents]"`. Scanned PDFs need external OCR and are
not supported. Imports are limited to the configured byte limit and 200,000
extracted characters; PDFs are limited to 100 pages and a 15-second extraction
timeout. DOCX imports read the main document body only.

Folder sync requires approval and scans at most 20 supported files across 500
directories. It indexes changed files lexically and removes index entries for
missing files only after all imports succeed; it never deletes originals. If a
changed document had semantic vectors, sync removes those stale vectors and
reports that semantic reindexing is needed. Existing unchanged vectors are kept.
Use `list knowledge` to inspect up to 100 entries and whether their files changed.
Stale or deleted sources are not returned in search. The unencrypted local SQLite
database contains document copies and vectors; clearing all memory also removes
the index. Retrieval uses an in-process cosine scan, not an external vector
database, and is not designed for large libraries.
See [architecture assessment](docs/ARCHITECTURE_ASSESSMENT.md).

## Recommended profiles

| Profile | Command | Intended use |
|---|---|---|
| Recommended, 4 GB RAM | `.\setup.cmd -Run` | GUI, tiny Whisper, Windows SAPI, exact volume support, 0.5B Q4 LLM |
| Lite + offline voice | `.\setup.cmd -Lite -SkipOllama -SkipSpeechDownload -Run` | Text tools, SQLite, and lightweight offline speech replies; no large model downloads |
| Everything experimental | `.\setup.cmd -Full -Run` | Adds PyTorch speaker recognition and openWakeWord; **not recommended for 4 GB RAM** |
| Setup without launching | `.\setup.cmd` | Install and test only |

Useful setup switches: `-SkipOllama`, `-SkipModelDownload`, `-SkipSpeechDownload`, and `-Dev`.

## What is implemented

- **Local AI brain:** loopback-only Ollama client that starts and verifies the local service automatically; `qwen2.5:0.5b-instruct-q4_K_M`; 2 CPU threads, 2048-token context, 256-token responses; model configurable in `config.toml`. Requests use Ollama's JSON mode, and a small model's slightly malformed JSON (unquoted chat text, plan steps without `arguments`, prose around the object, or plain prose) is recovered instead of discarded. Ollama is asked to unload the model after each response to reduce overlapping RAM use; later responses may start more slowly.
- **Offline voice input:** configurable microphone, explicit start/stop controls, faster-whisper `tiny` with INT8 CPU inference, or Vosk. Normal transcription uses cached Whisper files only; `setup-speech` explicitly permits the initial download. The STT model is released after each voice request to reduce RAM overlap.
- **Owner speaker profile:** 3–8 in-memory enrollment samples, local embeddings, conservative accept/reject/uncertain states, DPAPI-protected profile on Windows, no raw recordings saved.
- **Typed input:** same parser/planner/tool pipeline as voice, persistent command/conversation history.
- **Offline speech output:** typed requests, microphone requests, and wake-word replies now speak by default using interruptible Windows SAPI through `pyttsx3` (or a configured local Piper voice). Approval/PIN prompts use generic spoken cues and do not read sensitive arguments or PINs aloud. The full response remains visible; errors in the speaker are reported instead of silently claiming speech succeeded. Mute with `--no-speak` on the CLI, `/speak` during an interactive session, or the desktop Settings checkbox. `--speak` explicitly requires audio and reports failure if the device is unavailable. Existing muted settings remain private until you choose to enable speech: setup offers a `[y/N]` prompt, or use `hinata voice-output enable` (no config editing) or the desktop Settings checkbox. Use `hinata voice-output status` to check dependencies and `hinata voice-output test` to play a short offline sample. `hinata voice-output disable` saves an explicit mute choice for both desktop and terminal.
- **Controlled PC automation:** allow-listed apps, root-confined files/folders/search/read, active-window Unicode typing, hardware volume keys, optional exact volume through `pycaw`, basic system information.
- **Owner stop control:** the desktop emergency-stop button stops speech, recording, and wake listening, closes a pending local Ollama response, discards late desktop results and prevents pending approvals or a not-yet-started action while stop is active. A fresh request starts a new operation. A Windows action already executing may finish; HINATA reports that limitation rather than promising forcible rollback.
- **SQLite memory:** conversations, approved preferences, commands, task results, UI settings, and selected personality mode; inspect/search/export/delete controls.
- **Desktop UI:** microphone and text input, conversation view, listening/processing/speaking indicators, settings, enrollment, status, memory controls, and personality-mode selector. The dark-glass interface adds a softly animated perspective-grid scene, floating light orbs, a glossy HINATA sphere, translucent panels, and gradient controls with tactile press/focus states; it is painted locally without remote assets.
- **Six personality modes:** Friend, Advisor, Technical, Strategist, Professional, and Jarvis. Choose a mode in the desktop UI or say/type `switch to jarvis mode`. Modes adjust response style only and never modify permissions.
- **Controlled multi-step planning:** a local model may propose a configured bounded DAG (default five safe steps; the agentic configuration can allow up to 32 validated nodes). HINATA shows the complete plan, binds approval to its hash, delegates each node through a registered specialist, and keeps every side effect behind central authorization. Higher-impact actions (such as typing into an app, clearing memory, or closing apps) remain separately gated.
- **Wake word:** explicit `hinata wake` mode with bounded 80 ms streaming and cooldown for a user-supplied custom **Hey HINATA** ONNX model; disabled by default and never started automatically.

## Example commands

Structured commands bypass the LLM and continue working if it is unavailable:

```text
open notepad
close notepad
open folder Downloads
open file Documents\notes.txt
create folder Documents\Projects
read text file Documents\notes.txt
find files for quarterly report in Downloads
type "Hello from HINATA"
volume up
volume down
mute
set volume to 35
system info
remember that my editor is VS Code
switch to technical mode
list preferences
search memory for editor
clear conversations
clear memory
```

Other requests are sent to the **local** Ollama model, which must return a chat response, clarification, or valid tool JSON. Every proposed tool call is independently validated and authorized.

## Safety model

1. Understand the request.
2. Select a predefined tool or ask for clarification.
3. Validate exact tool names, argument types, lengths, and ranges.
4. Resolve paths inside approved roots and reject traversal/executable opening.
5. Check speaker status for enrolled voice input.
6. Request confirmation for closing apps, opening files, typing, and saving preferences.
7. Require confirmation **and a PIN** for memory erasure.
8. Execute with timeouts/bounds and report the actual result; never invent success.

Voice embeddings are biometric data and voice matching is probabilistic. It can be fooled by similar voices, replay, noise, and model error. **Voice is never accepted as a substitute for the PIN.** Typed local input is treated as the owner console, not as cryptographic authentication.

See [SECURITY.md](docs/SECURITY.md).

## Configuration

`setup.cmd` creates `config.toml`, which is ignored by Git. Edit it to change models, microphone, voice, roots, applications, or resource limits. Unknown settings and non-loopback Ollama hosts are rejected.

```toml
[ollama]
model = "qwen2.5:0.5b-instruct-q4_K_M"  # or "" to use any installed local model
threads = 2
context_tokens = 2048
auto_start = true   # start the local Ollama service automatically
auto_pull = false   # download the configured model once if it is missing

[speech]
whisper_model = "tiny"
whisper_compute_type = "int8"
# microphone = 0

[personality]
mode = "friend"  # friend, advisor, technical, strategist, professional

[security]
# allowed_roots = ['C:\Users\YOUR_NAME\Documents', 'C:\Users\YOUR_NAME\Downloads']
```

Default file roots are the current user's Desktop, Documents, and Downloads. A relative path such as `Projects\notes.txt` is resolved under those roots. A root-prefixed path such as `Documents\Projects\notes.txt` is also accepted. Add an application only as a fixed command array in `[applications.NAME]`; model text is never substituted into that command.

Private data defaults to `%LOCALAPPDATA%\HINATA` on Windows:

```text
hinata.db
speaker_profiles\owner.voice
speaker_profiles\owner.json
logs\hinata.log
```

Existing FRIDAY installs are reused in place when their old data directory/database is present; HINATA does not move or delete that data automatically. `HINATA_CONFIG` and `HINATA_DATA_DIR` override the new locations; the previous `FRIDAY_CONFIG` and `FRIDAY_DATA_DIR` variables remain recognized for migration. Do not put secrets in requests or preferences; conversation history intentionally stores what you ask.

## Memory controls

Set `[memory] conversation_limit` in `config.toml` (1–100000; default 1000) to
limit saved conversation messages. The oldest messages are pruned on the next
message, not immediately on changing the setting. This does not limit commands,
task results, preferences, log files, or exported backups; manage those separately.

```bat
.\hinata.cmd memory --recent 20
.\hinata.cmd memory --search "editor"
.\hinata.cmd memory --export hinata-memory.json
.\hinata.cmd memory --clear conversations
.\hinata.cmd memory --clear all
```

Exports omit settings and PIN hashes. Deletion requires the standard confirmation/PIN flow. The desktop **Memory** button provides the same inspection/export controls and routes deletion through the orchestrator.

## Optional components

### Vosk instead of faster-whisper

```bat
.\.venv\Scripts\python.exe -m pip install -e ".[light-speech]"
```

Download and extract a model from the [Vosk model page](https://alphacephei.com/vosk/models), then set:

```toml
[speech]
stt_backend = "vosk"
vosk_model_path = 'C:\hinata-models\vosk-model-small-en-us-0.15'
```

### Speaker enrollment

```bat
.\.venv\Scripts\python.exe -m pip install -e ".[speaker]"
.\hinata.cmd setup-pin
.\hinata.cmd enroll --samples 3 --duration 4
```

This optional dependency brings in PyTorch and has a much larger disk/RAM footprint than the default profile. The encoder is unloaded after verification/enrollment. Delete embeddings with `.\hinata.cmd delete-voice-profile`.

### Piper output

Piper is **not downloaded automatically**. Install a compatible local executable and voice, review the engine and voice-model licenses, then set `tts_backend = "piper"` and the three Piper paths in `config.toml`. The maintained `piper1-gpl` engine is GPL-3.0; the archived original was MIT. Voice licenses vary. Windows SAPI remains the zero-extra-download default.

### Hey HINATA wake word

Wake-word support is experimental and disabled to avoid continuous resource use. Install `.[speech,wake]`, train or otherwise legally obtain a custom `Hey HINATA` ONNX model, and configure `wake_word.enabled` and `wake_word.model_path`. Then run `.\hinata.cmd wake`; press **Ctrl+C** to stop. The listener pauses while each command is transcribed/executed. HINATA does not mislabel an unrelated pretrained phrase as “Hey HINATA.” Review openWakeWord model licenses separately from its Apache-2.0 code.

## Tests and verification

```bat
.\.venv\Scripts\python.exe scripts\run_tests.py
.\hinata.cmd --no-llm ask "system info"
.\hinata.cmd doctor
```

Tests cover schema and model-plan rejection, bounded plan approval/execution and stop handling, path traversal, bounded file operations, PIN hashing, memory search/export/deletion, approval flows, uncertain voice rejection, CLI recovery, optional speaker-profile logic, and desktop construction when PySide6 is installed. CI runs the portable suite on Ubuntu and Windows and builds the desktop offscreen on Windows.

Microphone quality, real speaker scores, SAPI/Piper audio, Windows focus/typing behavior, and Ollama model downloads still need validation on your physical Windows laptop. The portable test suite does not pretend to verify physical devices.

See [the staged installation and validation guide](docs/INSTALL_WINDOWS.md) for stage-by-stage commands, success criteria, and troubleshooting, and [roadmap status](docs/STAGE_STATUS.md) for gaps and a Windows resource measurement checklist.

## Repository layout

```text
hinata/
  main.py                  terminal entry point
  config.py                validated TOML configuration
  core/                    assistant, orchestrator, LLM, bounded planner, memory, personality, permissions
  agents/                  bounded replanner, postcondition reviewer, restricted dispatch workers
  tasks/                   persistent plan checkpoints and review state
  knowledge/               local document extraction, indexing and retrieval
  speech/                  microphone, STT, TTS, speaker, wake-word, voice pipeline
  automation/              apps, files, Windows input/audio, system information
  interface/               PySide6 windows, dialogs, widgets, dark styles
  database/                SQLite schema and safe export
  security/                schemas, PBKDF2 PIN, DPAPI/private-file protection
scripts/run_tests.py        portable verification
config.example.toml         documented defaults
setup.ps1 / setup.cmd       one-command Windows bootstrap
.hinata data is never committed
```

The core uses only the Python standard library. Optional AI/GUI packages are lazy-loaded so `hinata --no-llm` stays lightweight. HINATA is released under [MIT](LICENSE); third-party code/model notices are in [THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md).

### Specialist agents & coding workspaces

The orchestrator coordinates 10 specialized local agents (`orchestrator`,
`planner`, `research`, `coding`, `reviewer`, `memory`, `execution`, `document`,
`system_monitor`, and `voice`) via `hinata.agents.registry`.
Every agent has an explicit versioned tool allowlist, model role, memory scope,
risk ceiling, typed `AgentMessage` provenance, bounded delegation depth, cycle
detection, concurrency/resource locks, and evidence-bearing reports. Model-backed
roles use `AgentExecutor` for strict JSON preflight; they cannot execute tools or
approve themselves. Independent reviewer dispute resolution remains bounded and
is not a substitute for target-hardware validation:

- Use `inspect code Documents/script.py` for read-only AST inspection.
- Use `register workspace Documents/myrepo` to register a local coding workspace,
  index Python AST symbols (`hinata workspaces --symbols`), propose single-file or
  multi-file diffs, apply/revert patches atomically, and run unit tests inside a
  bounded, network-disabled subprocess sandbox (`hinata workspaces`, `hinata workspaces --patches`,
  backed by Linux `unshare -rn` + `RLIMIT`s, Windows `CreateJobObjectW` memory/process-tree
  limits, and a Python `sitecustomize` socket guard).
- Promote verified `COMPLETED` tasks into reusable parameterized offline workflow
  templates with `hinata playbooks --promote TASK_ID --name my-flow` and instantiate
  fresh approval-gated plans via `hinata playbooks --instantiate my-flow --param key=val`.
- Use `hinata agents` to view all 10 specialist manifests, permitted tools, and
  recent coordination activity.

### Inspecting plans, checkpoints, offline assets, and audit logs

Plans proposed by the local model use a cycle-checked DAG (`ExecutionPlan` /
`PlanNode`) that groups independent read-only steps into parallel waves while
binding interactive approval to the canonical SHA-256 `plan_hash`. Inspect tasks,
hardware profiles, offline asset SHA-256 bundles, and redacted trace timelines:

```text
hinata tasks
hinata tasks TASK_ID
hinata tasks TASK_ID --timeline
hinata tasks TASK_ID --pause
hinata tasks TASK_ID --resume-paused
hinata tasks TASK_ID --reconcile
hinata tasks TASK_ID --cancel
hinata tasks TASK_ID --verify
hinata tasks TASK_ID --reject
hinata model hardware
hinata model benchmark
hinata briefing
hinata override --status
hinata override --lockdown
hinata override --commander 10
hinata override --stand-down
hinata schedule --add "Morning Status" --request "executive briefing" --interval 3600
hinata playbooks
hinata workspaces --symbols
hinata assets --verify
hinata audit --verify-chain
hinata audit
```

`--cancel` only cancels a plan still awaiting approval. `--pause` pauses an
active task at a safe boundary (never mid-RUNNING step). An interrupted `RUNNING`
step has an **unknown outcome**: `--reconcile` inspects lease liveness and flags
uncertain checkpoints without ever replaying side effects automatically. Emergency stop remains cooperative and an in-flight action may
finish. Successful tool returns normally leave the task in `VERIFYING`, not
`COMPLETED`. A narrow exception is an exact request `ensure folder PATH exists`
(with a whitespace-free path) and a single approved create-folder step targeting
that same path. The exact form `ensure folders PATH1, PATH2 exist` also supports
up to five unique whitespace-free paths and matching create-folder steps. An
independent filesystem check can verify these objectives and record evidence
automatically. For other plans, a read-only reviewer checks
that created folders exist; unsupported postconditions are explicitly unknown. Review the original request,
steps, and checks with `hinata tasks TASK_ID --verify` or the desktop review
prompt. Only record completion after personally inspecting the result and entering
observed evidence. `--reject` records that the objective was not met. Human
attestation is not proof against a mistaken or dishonest reviewer, and generic
objectives beyond these exact folder forms are not automatically verified. Legacy tasks previously marked
completed from tool success are migrated to `VERIFYING` for review. Task metadata,
including requests, paths, arguments and attestations, is stored unencrypted in
SQLite. Clearing commands or all memory removes terminal tasks; pending, running
and verifying records are retained to avoid erasing uncertain side effects.

## Optional offline isolation

For Linux network namespaces and the separately configured Windows Sandbox
approach, including prerequisites, commands and security limitations, see
[offline isolation](docs/OFFLINE_ISOLATION.md). These launch paths are optional;
the normal launchers do not enforce operating-system network isolation.

### Lenovo LOQ i5 multi-AI fleet and local image creation (optional)

Run `setup.ps1` on Windows 11. Setup detects RAM, NVIDIA VRAM, and battery state,
shows the recommended Qwen + DeepSeek + reviewer + embedding model list, and asks
**`[y/N]` before downloading**. If you answer `y`, it pulls each model and then
updates only `[ollama]` in `config.toml` (backing up the previous config as
`config.toml.bak`). If any pull fails, the profile is **not** applied. Declining
keeps your current config; setup separately asks whether to download a baseline
chat model. `-SkipModelDownload` prevents the optional model and image downloads;
`-Lite` skips the large fleet and image backend. Non-interactive setup treats
unanswered prompts as **no**. Ollama 0.6.2+ is required for the multi-model fleet.

For **real offline PNGs**, setup separately asks permission to install optional
PyTorch/diffusers dependencies and download `stabilityai/sd-turbo` weights to the
private HINATA data directory (several GB of packages + model; check its model
license before opting in). On an RTX GPU with a compatible PyTorch CUDA build and
NVIDIA driver, generation runs locally; CPU is supported but substantially slower.
The one-shot image process uses only cached files and blocks Python network
connections. Qwen handles prompt direction; **SD-Turbo** renders the image. You
can alternatively use an existing A1111/Forge-compatible server on
`http://127.0.0.1:7860`. If neither renderer is available, HINATA saves a clearly
labeled `.svg` vector fallback rather than claiming it generated a photo. Output
is capped at 768px per side with the local model. `python -m hinata image status`
shows whether its weights are cached.

Without setup, `python -m hinata model fleet` prints a recommendation only; manual
`python -m hinata model pull --fleet` is an explicit connected download, and
`python -m hinata model apply-fleet --pack <pack_id>` applies a chosen installed
pack. Never use `apply-fleet` before the models are actually available. You can
also choose specific role models in `config.toml`. Roles are loaded one at a time
(`keep_alive=0`); the fleet does not run six models in VRAM concurrently.

`generate image <idea> to <path.png>` and
`build project <specification> in <empty-directory>` require owner approval,
write only to configured allowed roots, and run project checks in the offline
sandbox. The builder persists file, test, and diagnostic evidence and returns
truthful lifecycle states: a missing suite is `PARTIAL`, a failing check is
`FAILED`, and a deterministic fallback project is an explicitly labeled
scaffold, **not** a completed custom project.

**Autonomy boundary:** HINATA can plan, generate, execute approved local tools, and
run sandboxed project checks, but it does not take unlimited control of the PC or
guarantee that every task is finished correctly. Confirmation, PIN, and human
review remain in place even when voice replies are enabled.
