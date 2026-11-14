# Contributing to HINATA

Thank you for helping make local AI safer and easier to use. HINATA is alpha
software; small, tested improvements and reports from real machines are welcome.
Read the [roadmap](docs/STAGE_STATUS.md) and [security model](docs/SECURITY.md)
before making changes.

## Set up a development environment

Python 3.11+ is required. The core has no mandatory third-party dependencies;
GUI, speech, Ollama and document features have separate requirements.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev,documents]"
python -m unittest discover -s tests -q
ruff check hinata tests scripts
mypy hinata
```

For the Windows desktop, install `.[gui]`; audio and other extras are listed in
`pyproject.toml`. Do not download models or install heavy packages unless you
need them for your work. The suite skips optional features when dependencies or
platform capabilities are unavailable; report skips and real-device tests
separately from simulated tests. See the [offline isolation guide](docs/OFFLINE_ISOLATION.md)
for the opt-in real-model acceptance test.

## Making changes

1. For significant changes, open an issue first to discuss scope. Search existing
   issues before filing a duplicate.
2. Keep changes focused, add tests for behavior and failure paths, and run the
   checks above. On Windows, also run `python -m unittest tests.test_windows_scripts -v`
   when changing launchers. Never claim hardware or real-model validation from mocks.
3. Describe what changed, how to reproduce it, which tests ran, and what could not
   be tested. Add or update docs for configuration, security, and user-facing changes.
4. Open a pull request. Maintainers may request revisions or additional evidence.

Preserve explicit consent for downloads and sensitive actions, offline behavior,
allowlisted tools and path validation. Do not add arbitrary model-generated shell
execution or weaken approval boundaries. Do not commit secrets, local configs,
databases, user files, recordings or model weights. Avoid including private content
in issue logs or screenshots.

For potential vulnerabilities, use the [security reporting guidance](SECURITY.md)
instead of posting exploit details in a public issue.
