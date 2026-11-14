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
| 3. Text conversation | CLI, local model request, deterministic command pipeline. | Test against the real runtime; this is not a general-purpose runtime abstraction yet. |
| 4. Personality and memory | Five modes, SQLite conversations/preferences, search, export, deletion, configurable conversation retention. | Memory categories, editing and retention controls do not cover every requested category. SQLite is not encrypted. |
| 5. Windows automation | A small allowlist of apps, folder/file/search/read, system info, typing and volume. | Windows behavior needs hands-on testing. There is no general approved-script runner, file organizer, or universal per-tool timeout and verification framework. |
| 6. Speech recognition | Optional faster-whisper or Vosk, push-to-talk; offline-only model loading during normal transcription. | Prepare a model and test actual microphone/audio on Windows. |
| 7. Speaker recognition | Optional enrollment and embedding comparison; uncertain matches are rejected when enrolled. | Real owner/non-owner/replay tests, thresholds and RAM usage must be measured. Voice is not authentication. |
| 8. Voice output | Optional Windows SAPI or a separately installed Piper executable and voice. | Select and license a suitable feminine voice and test playback/interruption on Windows. No voice is bundled. |
| 9. Wake word | Optional streaming adapter, disabled by default. | Obtain/train a legally usable custom model, configure it, measure accuracy and resource use. No wake model is included. |
| 10. Desktop interface | PySide6 chat, mode, status, permission prompts and emergency stop. | Validate on Windows; an audio waveform and some requested controls are not implemented. |
| 11. Planning | Up to five allowlisted steps, durable checkpoints, one approval-gated revision, narrow folder-objective verification and human review. | Interrupted steps are uncertain and never automatically replayed. Generic objectives are not automatically verified; autonomous recovery remains incomplete. |
| 12. Reliability and security | Unit tests, schema checks, PIN, bounded paths and cooperative stop. | Test the full offline workflow, device integrations, failure cases and RAM on the target. There is no integrated safe-update/rollback system, no independent security audit, and actions already running may finish after stop. |

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
