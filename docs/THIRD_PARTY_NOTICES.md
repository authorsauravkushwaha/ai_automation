# Third-party dependency and model notices

Reviewed for the FRIDAY 0.1.0 integration on **2026-11-07**. FRIDAY does not bundle Ollama, Qt, PyTorch, Piper, model weights, or voice models in this Git repository. Package/model downloads retain their own licenses and notices. This document is integration guidance, not legal advice; recheck upstream terms when upgrading or redistributing.

| Component | Integration | Upstream license/notice |
|---|---|---|
| [Ollama](https://github.com/ollama/ollama) | Optional local runtime installed separately | MIT at review time |
| [Qwen2.5 0.5B Instruct / Ollama tag](https://ollama.com/library/qwen2.5:0.5b-instruct-q4_K_M) | Default local 494M-parameter Q4_K_M model | The Qwen2.5 model card lists 0.5B under Apache-2.0; retain model notices. Other Qwen sizes can differ (notably 3B/72B). |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Default optional offline STT | MIT code. Review the selected converted model card and upstream Whisper/model notices. |
| [OpenAI Whisper](https://github.com/openai/whisper) | Source architecture/weights for Whisper conversions | MIT code/model repository at review time; selected third-party conversions can add notices. |
| [Vosk API](https://github.com/alphacep/vosk-api) | Alternative STT | Apache-2.0 code. Check every downloaded model's license on the [model page](https://alphacephei.com/vosk/models); the small English 0.15 model is listed as Apache-2.0. |
| [Resemblyzer](https://github.com/resemble-ai/Resemblyzer) | Optional owner voice embeddings | Apache-2.0 project at review time. Review pretrained-weight and training-data terms before commercial redistribution. PyTorch and its dependencies have separate notices. |
| [PySide6 / Qt for Python](https://pypi.org/project/PySide6/) | Optional desktop UI | Package metadata lists LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; Qt also offers commercial licensing. Dynamic-linking/relocation and notice obligations apply when redistributing Qt. |
| [pyttsx3](https://github.com/nateshmbhat/pyttsx3) | Offline SAPI driver integration | MIT Python project; installed Windows voices have their own terms. |
| [python-sounddevice](https://github.com/spatialaudio/python-sounddevice) | Microphone capture | MIT Python code; PortAudio is separately licensed (MIT-like PortAudio notice). |
| [NumPy](https://github.com/numpy/numpy) | Audio/vector processing | BSD-3-Clause. |
| [pycaw](https://github.com/AndreMiras/pycaw) | Optional exact Windows endpoint volume | MIT project; `comtypes` and Windows audio APIs have their own notices/terms. |
| [openWakeWord](https://github.com/dscripka/openWakeWord) | Experimental optional wake-word inference | Apache-2.0 **code**. Official included/pretrained models are documented as **CC BY-NC-SA 4.0**, not Apache; custom model/data rights must be reviewed separately. |
| [Piper, maintained fork](https://github.com/OHF-Voice/piper1-gpl) | Optional external offline TTS executable | GPL-3.0 at review time. FRIDAY does not bundle or link this executable; invoking a user-installed executable does not remove its distribution obligations. |
| [Original Rhasspy Piper](https://github.com/rhasspy/piper) | Older external executable option | MIT, archived/unmaintained at review time. Do not confuse its license with the maintained GPL fork. |
| [Piper voices](https://huggingface.co/rhasspy/piper-voices) | Optional voice models | Licenses vary by voice and dataset. Read the selected `MODEL_CARD` before use or redistribution; no voice is downloaded automatically. |

## Installation choices made for this repository

- The core has no third-party runtime dependencies, keeping its license/security surface small.
- Default setup uses Ollama, the small Apache-2.0 Qwen2.5 variant, faster-whisper, Qt for Python, SAPI through pyttsx3, sounddevice/NumPy, and pycaw.
- Piper is not auto-downloaded because maintained code is GPL-3.0 and voices have separate terms.
- openWakeWord and Resemblyzer are opt-in because of resource use and model/weight considerations.
- No proprietary/paid API keys or cloud AI SDKs are included.

When distributing an installer/binary rather than this source-only checkout, run a new software bill of materials (SBOM), bundle all required license texts, verify model redistribution rights, and assess LGPL/GPL obligations for the exact artifacts shipped.
