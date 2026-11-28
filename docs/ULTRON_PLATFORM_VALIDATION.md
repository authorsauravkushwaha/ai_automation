# Ultron: actual platform and model validation

Ultron remains **enabled by default**. Saved policy, disabled inference, permission,
confirmation, PIN and lockdown decisions still take precedence. This report closes
specific environment-validation gaps; it does not certify all 100 audit findings.

## Results

| Validation | Observed result |
|---|---|
| Combined Linux suite with cryptography, actual Qt and Chromium enabled | **747 tests: 742 passed, 5 skipped; 71.528s** |
| Qt GUI | **33 tests passed**, including the Ultron button and disabled-inference behavior |
| Actual Chromium | **Acceptance passed locally and in CI**: same-origin form POST succeeds, forbidden-origin direct/redirect requests never reach the forbidden server |
| Windows/Linux CI | **All 15 jobs passed**, including Python 3.11–3.14 on both OSes, Windows Qt, hardening and browser checks |
| Actual local model | **16 evaluations completed, zero runtime/engine errors**; exact-answer scores below |
| Static checks | Ruff and Mypy (92 modules) passed; diff whitespace check passed |

Local tests used Python 3.11.2, PySide6 Essentials 6.11.3, Playwright 1.65.2 and
Chromium 158.0.8127.0 headless shell from `@sparticuz/chromium@158.0.0`. Qt used the
offscreen platform and test-only GL/DBus load shims, not a physical display. CI
installs Chromium through Playwright and runs Qt on Linux and Windows.

- [Final platform CI](https://github.com/authorsauravkushwaha/ai_automation/actions/runs/46171800935)
- [Windows cmd.exe/PowerShell launcher CI](https://github.com/authorsauravkushwaha/ai_automation/actions/runs/46171800980)
- [Recorded real-model evaluation](https://github.com/authorsauravkushwaha/ai_automation/actions/runs/46171404586)
- [Machine-readable platform evidence](audits/ultron-platform-validation.json)
- [Full model answers, traces, identities and timings](audits/ultron-real-model-validation.json)

Evidence identifies its tested commit. The model snapshot is `56dc8e3`; subsequent
changes fix Windows storage and test synchronization, not the inference engine or
benchmark corpus. Platform snapshot: `c97e305`. Original CI timestamps are retained
in the evidence rather than substituting an assumed date.

## Bugs found by actual execution

1. **Chromium redirects:** redirect hops need not re-enter Playwright routing.
   Validate `Location` before fulfilling a response, retain the dead fallback proxy,
   and test that the foreign server receives no request.
2. **Windows subprocess lifetime:** `exec` on Windows starts a replacement and
   exits the supervised bootstrap. Closing its kill-on-close Job Object could kill
   the command before it ran. The bootstrap now waits for the assigned child and
   propagates its exit status. A delayed-output/nonzero-exit regression accompanies
   actual timeout and output-quota tests. Assignment still fails closed.
3. **Windows backup durability:** `os.fsync`/CRT `_commit` rejects read-only file
   descriptors. Open the completed SQLite backup read/write before flushing; keep
   backup-first transactional migration and rollback checks intact.
4. **Windows key corruption:** CRT text-mode writes could expand LF bytes inside a
   DPAPI payload. Exclusive key creation now uses `O_BINARY` on Windows. A regression
   checks every byte value, and real DPAPI cross-instance reads exercise the OS API.
5. **Tests and cleanup:** SQLite fixtures explicitly close connections before
   Windows directory cleanup. The subprocess double models the Job boundary. GUI
   profile selection no longer incorrectly expects disabled inference to turn on.
   Cancellation tests wait for explicit entry and always release/join worker threads;
   slow CI startup is not misclassified as a cancellation failure. Cancellation and
   no-side-effect assertions remain intact.

## Actual model findings — no accuracy improvement claimed

Runtime: checksum-verified **Ollama 0.40.19**, CPU. Model:
`qwen2.5:0.5b-instruct-q4_K_M` (494.03M parameters), digest
`a8b0c51577010a279d933d14c2a8ab4b268079d44c5c8830c0a93900f1827c67`.
Preparation downloads the model explicitly; the evaluation itself disables automatic
service startup and model downloads. It uses only synthetic prompts, with no tools
or private user data.

| Mode | Exact-answer matches | Median elapsed time | Total model calls |
|---|---:|---:|---:|
| Off | **6 / 8** | **0.813s** | **8** |
| Ultra | **5 / 8** | **4.09s** | **39** |

Both modes failed unique-color counting and the Bengali year-only response. Ultra
also changed the correct `UNKNOWN` answer in the missing-evidence case to
`conference room`. **Ultra was slower and less accurate on this run.** Do not
interpret a green execution job as a passing quality threshold or a reason to
increase unattended permissions. The harness intentionally reports answer misses
without turning them into transport/runtime failures.

This is one repeat over eight exact-answer cases with counterbalanced mode order,
not a statistically meaningful general benchmark. No model RSS, full energy usage,
large-model quality, multilingual robustness or hardware-adaptive routing claim is
made. The user-requested default remains unchanged.

## Remaining scope limits

The five local skips are optional PDF extraction, the separate namespace-isolated
chat-plus-embedding acceptance test, and three Windows shell tests (covered by the
native Windows CI launcher job). The recorded model evaluation does **not** substitute
for the separate two-model namespace/RAG acceptance scenario. Physical display,
audio, GPU and target-laptop behavior remain outside this validation.

Existing limitations—cooperative in-process tool cancellation, incomplete OS native
network isolation, owner migration of existing encrypted data, and incomplete
optional dependency locking—remain documented in [the remediation tracker](ULTRON_REMEDIATION.md).
Historical audit/probe reports and earlier test checkpoints are preserved.
