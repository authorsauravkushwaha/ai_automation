# Roadmap status and remaining validation

This repository contains an **alpha implementation**, not a completed or independently
security-audited personal operating system. The portable suite uses mocks for many
OS and audio features. A passing test is not proof of Windows hardware behavior,
actual model quality, or fit within 4 GB RAM. See [INSTALL_WINDOWS.md](INSTALL_WINDOWS.md)
for staged setup and hands-on checks. Downloads require internet and explicit setup.

| Requested stage | Present in repository | What still needs work or target-machine validation |
|---|---|---|
| 1. Ollama | Loopback-only HTTP client plus an automatic runtime connector: `setup.ps1` installs, starts, and verifies Ollama, and the app restarts it on launch, on Reconnect, and before a chat request. | Confirm the hidden `ollama serve` start and the Reconnect badge on the target Windows machine. |
| 2. Small model | Qwen2.5 0.5B Q4 model is configurable and the setup can pull it. | Pull it, run real inference, measure performance and verify its answers locally. |
| 3. Text conversation | CLI, local model request, deterministic command pipeline, role-based local model routing, and model fallback events. Online-first web search uses public sources when enabled and falls back explicitly to local context when disconnected. | Test against the real runtime; no real-model evidence is retained by this audit. |
| 4. Personality and memory | Six modes, SQLite conversations/preferences, search, export, deletion, configurable conversation retention, and persistent owner name(s) used in replies. | Memory categories, editing and retention controls do not cover every requested category. SQLite is not encrypted by default. |
| 5. Windows automation | Configured apps plus cached Start Menu/Desktop, App Paths/Uninstall registry, built-in, and packaged-app discovery; folder/file/search/read, system info, typing and volume. | Windows behavior needs hands-on testing. There is no general approved-script runner, file organizer, or universal per-tool timeout and verification framework. |
| 6. Speech recognition | Optional faster-whisper or Vosk, with opt-in hands-free desktop voice; offline-only model loading during normal transcription. | Prepare a model and test actual microphone/audio on Windows. |
| 7. Speaker recognition | Optional enrollment and embedding comparison; uncertain matches are rejected when enrolled. | Real owner/non-owner/replay tests, thresholds and RAM usage must be measured. Voice is not authentication. |
| 8. Voice output | Optional Windows SAPI with soft-female voice selection by default, optional Edge neural voice path, or a separately installed Piper executable and voice. | Available SAPI voices vary; test playback/interruption on Windows. No voice is bundled. |
| 9. Wake word | Optional streaming adapter, disabled by default. | Obtain/train a legally usable custom model, configure it, measure accuracy and resource use. No wake model is included. |
| 10. Desktop interface | PySide6 chat, mode, status, permission prompts, emergency stop, owner-name/Web Search/voice preference settings, and up to four concurrent UI jobs. | Validate on Windows; an audio waveform and some requested controls are not implemented. Ollama inference remains serialized for memory safety. |
| 11. Planning and specialist runtime | Validated dependency DAGs (configured up to 32 steps), durable checkpoints, lease-bound claims, central permission dispatch, registered-agent delegation, strict model-backed specialist reports, and evidence-bearing reports. | Generic objectives are not automatically verified; lifecycle transitions, reviewer handoff, and autonomous repair/recovery remain incomplete. See [JARVIS_GAP_REGISTER.md](JARVIS_GAP_REGISTER.md). |
| 12. Project factory and verification | Polyglot code generation for 32 named languages, bounded workspace patches, isolated local test runner, truthful SUCCESS/PARTIAL/FAILED/BLOCKED/UNKNOWN_OUTCOME project results, and persistent project verification records. | Initial project generation is still a scaffold/template path rather than an iterative multi-day factory; non-Python compiler verification, package/release, and broad test adapters remain open. |
| 13. Reliability and security | Unit tests, schema checks, PIN, bounded paths, leases, idempotency records, audit chain, process-level offline guard, and cooperative stop. | A Python socket hook is not an OS firewall. Native child-process isolation, Windows target validation, external security review, and forcible cancellation of every native side effect remain open. |

## Measure the actual Windows resource budget

After setup, launch HINATA and run a representative text request, speech recognition,
and TTS one at a time. From a second PowerShell window sample the processes during
each step, including Ollama's worker where present:

```powershell
Get-Process python, pythonw, ollama, ollama_llama_server -ErrorAction SilentlyContinue |
  Select-Object ProcessName, Id, CPU,
    @{Name='WorkingSetMB';Expression={[math]::Round($_.WorkingSet64 / 1MB, 1)}},
    @{Name='PeakWorkingSetMB';Expression={[math]::Round($_.PeakWorkingSet64 / 1MB, 1)}}
Get-CimInstance Win32_OperatingSystem |
  Select-Object TotalVisibleMemorySize, FreePhysicalMemory
```

Record idle and peak readings, response times and failures for the actual laptop;
these are snapshots, not guaranteed total peak RAM. Model unloading reduces potential
overlap but can increase reload latency and does not guarantee a 4 GB fit. Close
unneeded applications before testing; opt-in speaker and wake packages can be heavy.
