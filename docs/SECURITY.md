# HINATA security and privacy notes

## Trust boundaries

HINATA treats the following as untrusted:

- Natural-language model output, including JSON enclosed in Markdown.
- Tool names, arguments, paths, application names, and file contents derived from a request.
- Voice identity scores, especially scores close to the configured threshold.
- Microphone/STT transcription errors.

Only predefined tools can execute. There is no `shell`, `eval`, PowerShell, arbitrary subprocess, or arbitrary Python tool exposed to the model. Application commands are fixed arrays in local configuration and run with `shell=False`.

## Authorization tiers

- **Safe:** allow-listed app launch, folder open, bounded search/read/create, volume, system information, memory search/list.
- **Confirmation:** app close, file open, active-window typing, saving preferences.
- **Sensitive:** memory deletion requires confirmation and a salted-hash-verified PIN.
- **Enrollment/profile replacement:** requires the PIN; raw samples are discarded after embedding.
- **Voice:** an enrolled profile must produce an accepted owner score. Rejected and near-threshold uncertain scores do not execute tools. A voice score never satisfies a PIN requirement. Spoken replies use generic approval/PIN cues, but ordinary responses may still be heard by nearby people. Mute via `hinata voice-output disable`, `--no-speak`, or desktop Settings before discussing private material. Older muted settings are not silently overridden; setup asks before enabling them.

The local typed console is treated as the intended owner interface. It is not proof that the person typing is physically the owner. Lock Windows when away.

## File and process controls

- Paths are canonicalized and confined to configured roots, including symlink/reparse-point resolution where supported by Python/Windows.
- Search has result, directory-count, and time bounds and does not follow directory symlinks.
- Text reads have extension, byte, and character limits.
- Executables, installers, shortcuts, and common scripts cannot be opened through `open_file`.
- Closing Explorer is disabled in the default application policy because it controls the Windows shell.
- App close is allow-listed and confirmation-gated; forced task killing is not requested by HINATA.
- Exact volume changes affect the default Windows audio endpoint. Hardware volume keys are device-dependent and are reported as key events, not fabricated numeric levels.

Allow-list only applications and roots you intend HINATA to control. Adding a whole drive or a powerful script interpreter expands the risk substantially and is discouraged.

## Data at rest

- SQLite stores conversations, commands, task results, preferences, and settings locally. By default SQLite rows are plaintext; when `security.encrypt_at_rest = true` is enabled in `config.toml`, structured memory values and encrypted JSON backups are sealed with `LocalVault` authenticated envelopes (`HMAC-SHA256` encrypt-then-MAC over PBKDF2-derived keys wrapped by Windows DPAPI or POSIX `0600` keyfiles). Setting `security.database_encryption_backend = "sqlcipher"` (with optional `pip install -e ".[encrypted-db]"`) additionally enables full-database SQLCipher page encryption (`PRAGMA key` + `PRAGMA cipher_memory_security = ON`), or `"auto"` to use SQLCipher when installed and `LocalVault` envelopes otherwise.
- Audit trace events (`agent_trace_events`) form an append-only SHA-256 cryptographic hash chain (`prev_hash` -> `event_hash`); run `hinata audit --verify-chain` to detect any modified, deleted, or reordered trace rows.
- Secret patterns (API keys, private keys, bearer tokens, GitHub/AWS/OpenAI/Slack tokens) are detected by `hinata.security.redaction`: plaintext secrets are blocked from unencrypted persistent memory storage and scrubbed automatically from `TraceRecorder` logs and audit exports.
- PINs are stored as PBKDF2-HMAC-SHA256 hashes with random salts and constant-time digest comparison. Plain PINs are not written to the database or logs.
- Memory JSON exports deliberately exclude settings/PIN material, but can contain private conversations and preferences. Protect/delete exports appropriately.
- Voice embeddings are protected with the current Windows user's DPAPI keys. Owner-only ACLs are applied best-effort to private paths. On non-Windows development systems, protection is owner-only filesystem permissions rather than DPAPI.
- No enrollment WAV files are created. Voice embeddings remain sensitive biometric-adjacent data even though they are not raw recordings.
- Tool output is shown in the UI but omitted from subsequent model conversation history; a short tool/status marker is retained instead. Read-file, knowledge-search and memory-search content is also omitted from command messages and task results. Typed-text command arguments are reduced to a character count in command metadata. Existing historical records created by older versions are not retroactively redacted.

## Network behavior

The normal assistant runtime endpoint is the configured Ollama URL, restricted to `127.0.0.1`, `localhost`, or `::1` over plain HTTP. The Ollama client ignores environment proxies and refuses HTTP redirects. HINATA has no analytics, crash reporter, cloud speech, cloud TTS, or silent remote-AI fallback. Any future Online Mode use must be explicitly configured with a domain allowlist and is not the default.

In addition, `OfflineGuard` (`hinata.security.offline_guard`) enforces runtime egress policy in Python:
- When `security.strict_offline = true` (or `online_mode_enabled = false`), non-loopback `socket.socket.connect` / `connect_ex` calls inside the HINATA process fail closed with `OfflinePolicyError`, and child subprocesses receive proxy-stripped offline environment flags (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `PIP_NO_INDEX=1`).
- Coding workspace tests executed through `IsolatedSandboxRunner` run inside `unshare -rn` on supported Linux kernels and inject a startup `sitecustomize.py` socket guard blocking all outbound network connections inside the Python test runner.
- Note that external native binaries (such as a separate host `ollama` daemon or launched desktop apps) are outside Python's in-process socket hook unless launched inside the OS namespace / Windows Sandbox wrapper.

Internet is used by explicit setup/download operations (`pip`, `winget`, `ollama pull`, initial faster-whisper model preparation, and optional SD-Turbo weights) and by any separate manual model download. Windows setup asks `[y/N]` separately before Ollama/fleet/chat-model, SD-Turbo, and speech-model downloads; EOF/non-interactive input does not grant consent. Normal faster-whisper transcription requires cached model files and does not initiate a download; use `hinata setup-speech` explicitly to prepare the model. Once models are cached, normal assistant operations are offline. Disabling Ollama does not disable local deterministic tools.

The optional local image model is downloaded only by the explicit `hinata image setup` command (or after consenting in `setup.ps1`). Inference loads local safetensors through diffusers in a one-shot subprocess with Hugging Face offline flags and Python socket connections disabled; this is **not an OS-level firewall against native libraries**. No external image service is contacted by the worker. The alternative A1111/Forge API is loopback-only, proxy-free, and redirect-denied. Model weights and images can contain sensitive material: review the model license, protect the data directory, and review generated images before sharing. `config.toml.bak*` files are ignored by Git but remain private copies of the prior settings.

## Operational recommendations

1. Use a non-administrator Windows account for normal use.
2. Enable BitLocker/device encryption and secure your Windows account.
3. Keep the default small roots and application allow-list.
4. Use a unique PIN longer than four digits; HINATA's PIN is separate from your Windows PIN.
5. Do not ask HINATA to type passwords, payment data, or other secrets into applications.
6. Review confirmation dialogs instead of automatically accepting them.
7. Test voice rejection with other speakers and recordings; never rely on it for high-value authorization.
8. Review third-party engine and model licenses before redistribution or commercial use.
9. Back up only the memory you intentionally need; securely delete old exports and voice profiles.
10. Keep Python, Ollama, and optional dependencies updated after testing compatibility.

## Limitations and incident response

This is a local productivity assistant, not a hardened security boundary against malware already running as your user, an administrator, an attacker with unlocked physical access, malicious audio played through your microphone, or a compromised model/runtime. LLM output can be wrong even when JSON is valid. File search/read and active-window typing can have unintended consequences if the wrong path/window is selected.

To stop activity in the desktop, click **Emergency stop**; it discards late desktop results and blocks pending actions until a fresh request starts; an already-running Windows action may finish. In the interactive terminal, `/stop` stops audio; Ctrl+C interrupts the current foreground command. To revoke voice use, run `hinata delete-voice-profile`. To erase local memory, use the PIN-protected memory controls and then remove any external exports/backups. To change the PIN, run `hinata setup-pin` and verify the current PIN.

## Local knowledge index

Indexing requires confirmation and stores document text unencrypted in SQLite.
Search excerpts are untrusted and are not replayed as model history. Excerpts
are nevertheless displayed in the UI/terminal; do not treat document content as
authoritative instructions. `remove document from index PATH` removes the copy,
not the source; PIN-protected `clear memory` removes all indexed copies as well.
Optional semantic indexing sends document text only to the configured loopback
Ollama service and stores vectors locally, unencrypted. Question answering sends
retrieved passages to that service as untrusted data, and treats the answer as
text only; citations and factual support must be checked by the user. Neither
prompt instructions nor citation-label validation prevent all prompt injection or
hallucinations. The coding verification runner has a best-effort Linux namespace/socket guard and bounded process group; ordinary model/Ollama processes and launched desktop applications are not placed inside that sandbox by HINATA. HINATA does not enforce an operating-system firewall for its entire process tree.

## Plan checkpoints

The exact steps, arguments, objective and source are stored in local unencrypted
SQLite before the interactive approval. The stored plan is atomically claimed
once; changing steps or approving under a different source is rejected. A step is
marked RUNNING before its action and recorded after it returns. This deliberately
leaves a RUNNING/unknown outcome on interruption, including interruption between
a successful side effect and its checkpoint. Nothing is automatically replayed
or resumed, even if the step might have been safe. A separate concurrent process
may still be executing a RUNNING task, so startup does not mark it as failed or
cancelled. Review the environment before issuing a new request. Coding verification
commands use the isolated workspace runner; existing desktop/file tools are not
retroactively converted into an OS-level sandbox. Clearing command history
removes completed/failed/cancelled checkpoints, but retains awaiting/running and
VERIFYING checkpoints; pending ones can first be cancelled with `hinata tasks ID --cancel`.
Successful action results alone do not complete the objective. For a single
create-folder step and the exact request `ensure folder PATH exists` (with a
whitespace-free path), the reviewer compares both resolved paths and rechecks
folder existence; the objective can then be recorded as automatically verified.
The exact comma-separated plural form `ensure folders PATH1, PATH2 exist` also
supports two to five unique whitespace-free paths, with a one-to-one match to
completed create-folder steps. Otherwise it marks unsupported checks unknown, and a human must inspect the
original request and supply evidence before marking complete. The CLI and desktop
review paths recheck folder postconditions. Attestations are not cryptographic
proof; a mistaken user can still assert an incorrect result. Legacy completed
tasks without such attestations are migrated to VERIFYING. Worker events store
only worker name, tool name, and status, locally in SQLite. All workers are called
only after central validation and authorization; worker allowlists do not grant
additional permissions.

## Optional OS-level isolation

See [offline isolation](OFFLINE_ISOLATION.md) for the Linux namespace launcher
and Windows Sandbox configuration generator, including important IPC, shared
folder and host-process limitations. The Linux launcher is tested with an actual
namespace in this environment. The Windows configuration is generated and
unit-tested, but has not been run on Windows. Neither mechanism monitors all
attempted external connections, and ordinary HINATA launches are not isolated.

## Document ingestion

Local PDF import uses the optional pypdf package in a separate process with a
15-second wall timeout, bounded input and extracted text, a 100-page cap, and
POSIX CPU/address-space limits where supported. It is not an OS sandbox, and
Windows does not receive the POSIX memory/CPU limits. Do not assume malicious
PDFs are harmless. DOCX extraction limits the compressed input and main XML,
rejects XML DTD/entity declarations, reads no external resources and executes no
macros. Both formats are parsed locally. PDF text extraction is not OCR; DOCX
headers, comments and embedded objects are not indexed. An approved folder sync
can commit some successful imports before an error; it only removes missing
index entries when every import succeeds. Original documents are never deleted.

## Code inspection

`inspect code PATH` statically parses Python source under allowed roots, bounded to
20,000 characters, without importing or running it. It reports a few suspicious
call patterns, not a comprehensive security assessment; aliases, dynamic calls and
many other vulnerabilities can be missed. It does not grant permission to execute
or modify code. Output identifiers may contain sensitive project metadata and are
stored in local command results. Filesystem checks are still subject to path races.
