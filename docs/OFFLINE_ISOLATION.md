# Offline isolation: Linux first, Windows second

These optional launch paths do not make HINATA production-grade. They do not
replace a full offline acceptance test on the target laptop. Download runtimes,
Python packages, LLMs, embedding models and speech assets during connected setup;
no assets are bundled in this repository.

## Linux: network namespace

Linux needs Python 3.11+, `unshare` (util-linux), `ip` (iproute2), and kernel
support for unprivileged user and network namespaces. Some distributions or
container hosts disable them. The launcher fails rather than silently running
without isolation. Prepare HINATA and Ollama models first and set
`ollama.auto_pull = false` in your config. Then:

```bash
python scripts/offline_linux.py --check
python scripts/offline_linux.py --with-ollama -- gui
# Or use the interactive console instead of the GUI:
python scripts/offline_linux.py --with-ollama
# Or run a deterministic command without a model:
python scripts/offline_linux.py -- --no-llm ask system info
# Optional config goes before --:
python scripts/offline_linux.py --config /path/to/config.toml --with-ollama -- ask hello
```

`--with-ollama` starts a separate `ollama serve` *inside* the namespace using the
installed executable and preloaded model store. The host's Ollama service is not
reachable over its loopback address from the namespace. Without this switch,
no separate server is explicitly started by the launcher; if normal HINATA
`auto_start` is enabled, it may start its own service within the namespace.
The launcher checks that loopback works and its IPv4/IPv6 probe to reserved
remote addresses is unroutable. It rejects `auto_pull = true` and forwarded
`--config` overrides. `--check` probes isolation only and does not load config,
models, GUI, or audio. It does not monitor every attempted connection.

The namespace restricts ordinary IP networking for processes that stay inside
it, including the isolated Ollama server. It is **not a complete security
sandbox**: the filesystem is shared, and processes may reach host services via
Unix-domain sockets or other IPC and ask them to communicate externally. Apps
launched through an existing host service may execute outside the namespace.
A malicious or privileged local actor can defeat these boundaries. Use a VM with
networking disabled for stronger separation, avoid shared IPC, and do not run
untrusted applications. The default `hinata` and `start.bat` entry points do not
use this launcher.

## Windows: Windows Sandbox, networking disabled

Windows Sandbox is an optional Windows feature requiring a supported edition,
virtualization, and administrator-enabled Windows Sandbox. It is not available
on all Windows systems. The provided generator builds a `.wsb` file with
`Networking` disabled, clipboard and printer redirection disabled, and only the
explicit data directory writable. It **has not been tested in an actual Windows
Sandbox in this environment**. Review and validate the generated file and the VM
on the target Windows installation. A normal HINATA Windows launch is not
network-isolated.

Before going offline, prepare six **separate, non-overlapping** host directories:

1. `project`: this repository, including `scripts/windows_sandbox_start.cmd`.
2. `python`: a self-contained Windows Python installation (not a relocated
   virtual environment), with GUI and desired speech dependencies already
   installed in its own site-packages. Test imports locally. The mapped project is
   used via `PYTHONPATH`; the sandbox never installs packages.
3. `ollama`: the full Ollama installation directory containing `ollama.exe` and
   its required libraries.
4. `models`: the populated Ollama model store containing `manifests` and `blobs`,
   copied after explicitly pulling the desired chat and embedding models. For
   Whisper, additionally place a complete local faster-whisper model under
   `models/whisper-tiny`, or adjust the sandbox config to a locally mapped path.
5. `data`: a private writable directory. Copy `config.windows-sandbox.example.toml`
   here as `offline-config.toml` and customize model names, language and speech
   paths. The startup script requires the loopback URL and `auto_pull = false`.
6. `documents`: files you want the assistant to read; this mapping is read-only.

For example, from an already prepared checkout on Windows:

```powershell
python scripts\windows_sandbox_config.py `
  --project C:\HinataOffline\project --python C:\HinataOffline\python `
  --ollama C:\HinataOffline\ollama --models C:\HinataOffline\models `
  --data C:\HinataOffline\data --documents C:\HinataOffline\documents `
  --output C:\HinataOffline\hinata-offline.wsb
```

Review the generated XML and double-click the `.wsb` file to start Windows
Sandbox. Add `--enable-audio` only if microphone redirection is desired; it is
disabled by default. The script maps files and models read-only, starts a local
Ollama server inside the sandbox, then launches HINATA. The private data directory
persists on the host. The sandbox filesystem otherwise disappears when closed.
If a component is missing, exit and prepare it on the host; do not try to install
or download inside the offline VM. Verify GUI, loopback inference, document access,
TTS, and microphone individually on the target machine. Hardware acceleration,
audio redirection, Windows Sandbox paths, and installed dependencies vary.

The writable host data mapping is an intentional shared channel: anything with
access to the sandbox can write to it, and a host process could relay those files
elsewhere. Read-only mappings also expose their content to software in the VM.
Do not mount secrets or use a shared folder as a communication bridge if strict
isolation is needed. Windows Sandbox is not a defense against a malicious or
compromised host, and `.wsb` networking isolation is not continuous monitoring of
attempted external connections.

## What was actually tested here

On Linux, automated tests create real user/network namespaces, verify isolated
loopback and blocked remote routes, and demonstrate that a host loopback server
is unreachable. A multi-process workflow tests chat, semantic retrieval, memory,
and file creation with a **fake** loopback Ollama protocol server. It is not a
real LLM, embedding-quality, microphone, speech generation or full offline
acceptance test. Windows XML generation is unit-tested only. Real installed
models, voice devices, Windows Sandbox, and a disconnected target laptop still
require hands-on validation.

## Opt-in real-model acceptance test (Linux)

With Ollama and both named models already installed, run the following on a Linux
host that permits the namespace launcher. No model is downloaded by this test.
It uses a temporary document and database, starts a separate isolated Ollama
server for each command, and exercises actual chat, embeddings, retrieval,
memory across restarts and a filesystem tool. It fails if the requested models,
namespace, or workflow are unavailable; the regular suite skips it by default.

```bash
HINATA_REAL_OFFLINE_ACCEPTANCE=1 \
HINATA_ACCEPTANCE_CHAT_MODEL='your-preloaded-chat-model' \
HINATA_ACCEPTANCE_EMBED_MODEL='your-preloaded-embedding-model' \
python -m unittest tests.test_real_offline_acceptance -v
```

A pass does not evaluate model answer quality, speech hardware, Windows, GUI, or
all possible network paths. The namespace caveats above still apply. This test
has not been run against real models in the current environment.
