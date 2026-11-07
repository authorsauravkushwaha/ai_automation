# HINATA — Personal Offline AI Assistant for Windows 11

HINATA is a local-first, permission-controlled desktop assistant designed for a **Windows 11 laptop with 4 GB RAM**. It combines a quantized Ollama model, deterministic natural-language commands, offline speech, optional owner voice verification, SQLite memory, a dark PySide6 interface, and bounded Windows automation.

**No paid APIs. No OpenAI dependency. No cloud conversation service. No arbitrary model-generated shell execution.**

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

If `setup.cmd` stops with parser errors such as `Unexpected token '\Scripts\python.exe"' in expression or statement` or `The string is missing the terminator`, the checkout predates the script encoding fix. Update it and run setup again:

```bat
git pull
.\setup.cmd -Run
```

The bootstrap scripts are intentionally ASCII-only so that Windows PowerShell 5.1 (`powershell.exe`, which `setup.cmd` starts) decodes them the same way PowerShell 7 does.

### After setup

```bat
.\hinata.cmd gui                 :: desktop interface
.\hinata.cmd                     :: interactive terminal
.\hinata.cmd ask "system info"   :: one typed request
.\hinata.cmd listen              :: one 5-second voice request
.\hinata.cmd doctor              :: local diagnostics
.\hinata.cmd status              :: JSON feature/model status
```

Set a PIN before sensitive operations or voice enrollment:

```bat
.\hinata.cmd setup-pin
```

> Setup downloads packages and models. Once those downloads finish, normal chat, speech recognition, speech output, memory, and automation do not require internet. Structured commands also work when Ollama is stopped.

## Recommended profiles

| Profile | Command | Intended use |
|---|---|---|
| Recommended, 4 GB RAM | `.\setup.cmd -Run` | GUI, tiny Whisper, Windows SAPI, exact volume support, 0.5B Q4 LLM |
| Core only | `.\setup.cmd -Lite -SkipOllama -SkipSpeechDownload -Run` | Text tools and SQLite; no heavy runtime downloads |
| Everything experimental | `.\setup.cmd -Full -Run` | Adds PyTorch speaker recognition and openWakeWord; **not recommended for 4 GB RAM** |
| Setup without launching | `.\setup.cmd` | Install and test only |

Useful setup switches: `-SkipOllama`, `-SkipModelDownload`, `-SkipSpeechDownload`, and `-Dev`.

## What is implemented

- **Local AI brain:** loopback-only Ollama client; `qwen2.5:0.5b-instruct-q4_K_M`; 2 CPU threads, 2048-token context, 256-token responses; model configurable in `config.toml`. Ollama is asked to unload it after each response to reduce overlapping RAM use; later responses may start more slowly.
- **Offline voice input:** configurable microphone, explicit start/stop controls, faster-whisper `tiny` with INT8 CPU inference, or Vosk. Normal transcription uses cached Whisper files only; `setup-speech` explicitly permits the initial download. The STT model is released after each voice request to reduce RAM overlap.
- **Owner speaker profile:** 3–8 in-memory enrollment samples, local embeddings, conservative accept/reject/uncertain states, DPAPI-protected profile on Windows, no raw recordings saved.
- **Typed input:** same parser/planner/tool pipeline as voice, persistent command/conversation history.
- **Offline speech output:** interruptible Windows SAPI through `pyttsx3`; optional Piper executable support with configured local voice.
- **Controlled PC automation:** allow-listed apps, root-confined files/folders/search/read, active-window Unicode typing, hardware volume keys, optional exact volume through `pycaw`, basic system information.
- **Owner stop control:** the desktop emergency-stop button stops speech, recording, and wake listening, closes a pending local Ollama response, discards late desktop results and prevents pending approvals or a not-yet-started action while stop is active. A fresh request starts a new operation. A Windows action already executing may finish; HINATA reports that limitation rather than promising forcible rollback.
- **SQLite memory:** conversations, approved preferences, commands, task results, UI settings, and selected personality mode; inspect/search/export/delete controls.
- **Desktop UI:** microphone and text input, conversation view, listening/processing/speaking indicators, settings, enrollment, status, memory controls, personality-mode selector, and dark theme.
- **Five personality modes:** Friend, Advisor, Technical, Strategist, and Professional. Choose a mode in the desktop UI or say/type `switch to technical mode`. Modes adjust response style only and never modify permissions.
- **Controlled multi-step planning:** a local model may propose up to five schema-validated low-impact tool steps. HINATA shows the complete plan and waits for one explicit owner approval before execution. Higher-impact actions (such as typing into an app, clearing memory, or closing apps) are excluded from batch plans and remain separately gated.
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
model = "qwen2.5:0.5b-instruct-q4_K_M"
threads = 2
context_tokens = 2048

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
