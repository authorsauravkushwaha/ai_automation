# HINATA staged installation and validation guide (Windows 11)

If you downloaded the repository as a ZIP from GitHub, you can skip the command-line bootstrap: extract the ZIP, open the extracted `ai_automation-main` folder, and double-click **`start.bat`**. The first run performs the complete setup (installing Python 3.11+ via `winget` if you agree, the environment, Ollama, and the model downloads) and then opens the desktop window. On later runs, `start.bat` checks the required GUI, Whisper or Vosk speech backend, text-to-speech, Windows audio controls, and configured offline speech model; it installs or downloads anything missing before launching HINATA. Keep an internet connection available if a repair is needed. If everything is present, it only checks/starts Ollama as needed and opens the app. The rest of this guide verifies each layer manually instead of running the complete `setup.cmd -Run` bootstrap. Complete source files are already saved in this repository; do not create a parallel copy of the package. Run commands from the repository root in PowerShell unless a command is specifically for Command Prompt.

## 0. Inspect the machine and checkout

**Objective:** confirm the actual resources before loading models.

```powershell
git clone https://github.com/authorsauravkushwaha/ai_automation.git
cd ai_automation
git status
Get-ComputerInfo | Select-Object WindowsProductName, OsArchitecture, CsTotalPhysicalMemory
Get-CimInstance Win32_Processor | Select-Object Name, NumberOfLogicalProcessors
Get-PSDrive C
py -3.11 --version
```

Expected: Windows 11, 64-bit Python 3.11 or newer, enough free disk space. On a 4 GB machine, close unnecessary applications and retain the default tiny/small models. GPU acceleration is neither required nor assumed.

If `py` is unavailable:

```powershell
winget install -e --id Python.Python.3.11
```

Open a new terminal after installation.

## STAGE 1 — Ollama installation and verification

**Objective:** install only the local inference runtime. No HINATA code depends on a remote AI endpoint.

**Dependency:** Ollama for Windows. The bootstrap uses this command:

```powershell
winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
```

Alternative: install from <https://ollama.com/download/windows>. Open a new terminal, then:

```powershell
ollama --version
Invoke-RestMethod http://127.0.0.1:11434/api/tags | ConvertTo-Json -Depth 5
```

Expected: a version and a JSON response (possibly with an empty model list).

`setup.ps1` performs this step for you: it locates `ollama.exe` (including the default
per-user install folder when PATH has not been refreshed yet), starts `ollama serve`
hidden on `127.0.0.1:11434`, and waits for the API to answer. HINATA repeats the same
automatic start whenever the desktop window opens, when **Reconnect** is pressed, and
before a chat request if the service was stopped in the meantime. Manual
`ollama serve` is therefore a fallback for diagnosis, not a required step.

Relevant HINATA files: `hinata/core/ollama_service.py` (find/start/verify the local
runtime) and `hinata/core/llm_manager.py` (loopback-only client). The configuration
rejects non-loopback hosts.

## STAGE 2 — Local model installation and inference test

**Objective:** download and run the explicit small quantized model.

```powershell
ollama pull qwen2.5:0.5b-instruct-q4_K_M
ollama run qwen2.5:0.5b-instruct-q4_K_M "Return only this JSON: {\"type\":\"chat\",\"content\":\"local test passed\"}"
ollama show qwen2.5:0.5b-instruct-q4_K_M
.\.venv\Scripts\python.exe -m hinata model status
```

`hinata model status` connects automatically and prints the runtime state
(`ready`, `model_missing`, `not_installed`, `unreachable`, or `disabled`), the
detected executable, and every installed model. `hinata doctor` reports the same
connection state.

Expected: a local response and model metadata showing the selected quantization. This setup step needs internet; subsequent inference does not. Do not upgrade to a larger model on 4 GB RAM without measuring memory first.

If download fails, rerun `ollama pull`; if inference fails, confirm the Ollama service remains running and inspect its logs. Do not continue by substituting a paid API.

## STAGE 3 — Text-based assistant

**Objective:** install the zero-third-party-dependency core and verify real SQLite/tool results.

**Files:** `hinata/main.py`, `hinata/config.py`, `hinata/core/*`, `hinata/database/database.py`, `hinata/security/*`, and `config.example.toml`.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item config.example.toml config.toml   # Skip if config.toml already exists.
.\.venv\Scripts\python.exe scripts\run_tests.py
.\.venv\Scripts\python.exe -m hinata --no-llm ask "system info"
.\.venv\Scripts\python.exe -m hinata model status
```

Expected: all tests pass, actual OS/CPU/RAM/disk data is printed, and Ollama reports the downloaded model as installed. `--no-llm` proves the deterministic pipeline independently.

For natural-language chat:

```powershell
.\.venv\Scripts\python.exe -m hinata ask "Introduce yourself in one sentence"
.\.venv\Scripts\python.exe -m hinata
```

If the model is unavailable, structured commands still work and HINATA must report the local-model error rather than fabricate a response.

## STAGE 4 — Basic Windows automation

**Objective:** test allow-listed operations one at a time.

**Files:** `hinata/automation/*`, `hinata/core/permissions.py`, `hinata/security/action_validator.py`.

```powershell
.\hinata.cmd ask "open notepad"
.\hinata.cmd ask "create folder Documents\HINATA-Test"
.\hinata.cmd ask "find files for HINATA in Documents"
.\hinata.cmd ask "close notepad"
```

Expected: Notepad launches, the folder actually appears under Documents, search returns real paths, and close asks for confirmation first. Open a permitted text file only after creating a harmless test file:

```powershell
Set-Content "$env:USERPROFILE\Documents\HINATA-Test\note.txt" "offline test"
.\hinata.cmd ask "read text file Documents\HINATA-Test\note.txt"
```

Test typing only with a blank Notepad window focused, and confirm the prompt:

```powershell
.\hinata.cmd ask 'type "HINATA controlled typing test"'
```

Do not test on an elevated application from a non-elevated HINATA process; Windows blocks that input correctly. Executable/script opening and paths outside configured roots must fail.

## STAGE 5 — Offline speech recognition

**Objective:** capture microphone audio and transcribe it locally with a small CPU model.

**Dependencies:** `sounddevice`, NumPy, faster-whisper/CTranslate2, and the tiny model cache. No separately installed FFmpeg is required for the in-memory microphone path.

**Files:** `hinata/speech/microphone.py`, `speech_to_text.py`, and `voice_pipeline.py`.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[speech]"
.\.venv\Scripts\python.exe -m hinata devices
.\.venv\Scripts\python.exe -m hinata setup-speech   # First model download uses internet.
.\.venv\Scripts\python.exe -m hinata listen --duration 5
```

Expected: input devices are listed, the first model preparation succeeds, and a spoken request such as “system info” produces a transcript and actual system data. To select a device, set `speech.microphone` in `config.toml` to an index from `devices`.

Normal transcription does not download missing Whisper weights; run `setup-speech` explicitly first. The speech model is unloaded after each voice request, and Ollama is asked to unload its model after each response. This reduces potential RAM overlap at the cost of reload latency; measure actual usage on the target laptop.

For a lighter alternative, install `.[light-speech]`, download/extract a Vosk model, and set `stt_backend = "vosk"` plus `vosk_model_path`. Test again. Diagnose microphone permissions under **Settings → Privacy & security → Microphone** before changing models.

## STAGE 6 — Speaker enrollment and recognition

**Objective:** create a protected owner profile without retaining raw recordings.

**Warning:** this optional Resemblyzer/PyTorch path is substantially heavier than the default profile. Measure free RAM and close the GUI/other models first if necessary.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[speaker]"
.\hinata.cmd setup-pin
.\hinata.cmd enroll --samples 3 --duration 4
.\hinata.cmd listen --duration 5
```

Expected: PIN verification precedes enrollment; three samples are captured in memory; `owner.voice` and metadata appear under the private data directory; no WAV files are produced. The enrolled owner's score should be accepted, while non-owner/uncertain scores must be rejected before transcription/execution. Sensitive operations still request the PIN.

Adjust `speaker_threshold` only after recording genuine owner/non-owner test scores. Do not lower it merely to force acceptance. Test playback/replay resistance separately; this feature is not authentication. Remove the profile with `hinata delete-voice-profile`.

## STAGE 7 — Offline speech output

**Objective:** speak and interrupt responses without a voice API.

**Default Windows SAPI path:**

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[tts]"
.\hinata.cmd --no-llm --speak ask "system info"
.\hinata.cmd --no-llm
# In the shell: /speak, then /stop to interrupt.
```

Expected: an installed Windows voice reads the result; `/stop` interrupts it. Set `speech.tts_voice` to an installed voice name/ID substring if desired.

**Optional Piper path:** install a compatible executable and voice separately, review `docs/THIRD_PARTY_NOTICES.md`, and set `tts_backend = "piper"`, `piper_executable`, `piper_model`, and `piper_config`. Test with the same `--speak` command. Piper is not silently downloaded because engine and voice licenses differ.

## STAGE 8 — Wake-word functionality

**Objective:** make the optional openWakeWord adapter available without forcing an always-on process.

**Files:** `hinata/speech/wake_word.py` and `[wake_word]` in `config.toml`.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[wake]"
```

Supply a legally obtained/custom-trained **Hey HINATA** ONNX model, then configure:

```toml
[wake_word]
enabled = true
model_path = 'C:\hinata-models\hey_hinata.onnx'
threshold = 0.5
cooldown_seconds = 2.0
```

Start the explicit listener and speak the configured phrase, then give a short command:

```powershell
.\hinata.cmd wake --duration 5
# Press Ctrl+C to stop the always-on listener.
```

The service loads on demand, streams bounded 80 ms / 16 kHz chunks, applies a confidence threshold and cooldown, and pauses wake listening while each command is recorded/transcribed/executed. HINATA never starts this service automatically and does not substitute an unrelated pretrained wake phrase. Benchmark CPU/RAM and false accepts before routine use. Official pretrained openWakeWord models have separate noncommercial/share-alike terms; review them.

## STAGE 9 — Desktop interface

**Objective:** verify a responsive UI using the same tested pipeline.

**Files:** `hinata/interface/main_window.py`, `dialogs.py`, `components.py`, and `styles.py`.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[gui]"
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe scripts\run_tests.py
Remove-Item Env:QT_QPA_PLATFORM
.\hinata.cmd gui
```

Expected: dark main window, text/microphone controls, conversation, three activity indicators, settings, enrollment, memory, and system/model status. Send `system info`, then a confirmation-gated command. Confirm that long LLM/STT operations run in the background and the window cannot close midway through a tool task.

## STAGE 10 — Memory, reliability, offline integration

**Objective:** verify persistence, deletion, recovery, and normal offline operation.

```powershell
.\hinata.cmd setup-pin
.\hinata.cmd ask "remember that my editor is VS Code"
.\hinata.cmd memory --search editor
.\hinata.cmd memory --export .\hinata-memory.json
.\hinata.cmd memory --clear preferences
.\hinata.cmd doctor
.\.venv\Scripts\python.exe scripts\run_tests.py
```

Restart HINATA and search again before deletion to confirm persistence. Inspect the export: settings and PIN material must be absent. Deletion must require confirmation and the correct PIN.

Finally, after all model downloads are complete, temporarily disconnect from the internet and repeat:

```powershell
.\hinata.cmd ask "system info"
.\hinata.cmd ask "Introduce yourself in one sentence"
.\hinata.cmd listen --duration 5
.\hinata.cmd --no-llm --speak ask "system info"
```

Expected: local Ollama chat, downloaded STT, SAPI, memory, and tools continue to work. Features that were not downloaded/configured must produce a clear local error, not attempt a cloud fallback.

## Resource check and cleanup

```powershell
Get-Process python, ollama -ErrorAction SilentlyContinue | Select-Object Name, CPU, WorkingSet64
.\hinata.cmd status
```

On 4 GB RAM, keep `threads = 2`, `context_tokens = 2048`, `whisper_model = "tiny"`, and avoid the speaker/wake extras during normal use. Stop speaking/listening from the UI or `/stop`. Private data remains under `%LOCALAPPDATA%\HINATA`; uninstalling Python packages does not delete it. Delete that folder explicitly only after exporting anything you need.
