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
- **Voice:** an enrolled profile must produce an accepted owner score. Rejected and near-threshold uncertain scores do not execute tools. A voice score never satisfies a PIN requirement.

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

- SQLite stores conversations, commands, task results, preferences, and settings locally. It is **not an encrypted database**. Use Windows device encryption/BitLocker and a protected Windows account for stronger at-rest protection.
- PINs are stored as PBKDF2-HMAC-SHA256 hashes with random salts and constant-time digest comparison. Plain PINs are not written to the database or logs.
- Memory JSON exports deliberately exclude settings/PIN material, but can contain private conversations and preferences. Protect/delete exports appropriately.
- Voice embeddings are protected with the current Windows user's DPAPI keys. Owner-only ACLs are applied best-effort to private paths. On non-Windows development systems, protection is owner-only filesystem permissions rather than DPAPI.
- No enrollment WAV files are created. Voice embeddings remain sensitive biometric-adjacent data even though they are not raw recordings.
- Read-file content is omitted from `task_results`; it can still appear in the visible conversation/history because that is the requested result. Typed-text command arguments are reduced to a character count in command metadata.

## Network behavior

The only assistant runtime endpoint is the configured Ollama URL, restricted to `127.0.0.1`, `localhost`, or `::1` over plain HTTP. HINATA has no analytics, crash reporter, cloud speech, cloud TTS, or remote-AI fallback.

Internet is used by explicit setup/download operations (`pip`, `winget`, `ollama pull`, initial faster-whisper model preparation) and by any separate manual model download. Once models are cached, normal assistant operations are offline. Disabling Ollama does not disable local deterministic tools.

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

To stop activity: click **Stop audio**, use `/stop`, or close HINATA after the active tool finishes. To revoke voice use, run `hinata delete-voice-profile`. To erase local memory, use the PIN-protected memory controls and then remove any external exports/backups. To change the PIN, run `hinata setup-pin` and verify the current PIN.
