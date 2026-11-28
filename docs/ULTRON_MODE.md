# Ultron mode: deeper reasoning, bounded automation

Ultron is the **default startup preset** for HINATA, not a new model. It combines
existing ultra deliberation with low-risk autonomy and adds a continuously running
scheduler with durable dispatch protection. It does not download models, change
your saved configuration, remove permissions, or grant a Commander Lease.

## Start it

From the repository (Python 3.11+, configured local Ollama model):

```sh
python -m hinata
python -m hinata ask "Review my project requirements and propose a testable implementation plan"
python -m hinata gui
python -m hinata status --no-model-check
```

In the interactive terminal:

```text
/ultron on
/ultron status
/ultron off
```

No activation flag is needed: terminal, desktop and voice sessions start with
Ultron enabled. Explicit saved thinking/autonomy values take precedence over the
preset (including `off`, `SUGGEST_ONLY`, and the values in the example config).
`/ultron off` restores only fields still owned by the preset, preserving changes
you made after enabling it. Repeated `on` reapplies the effective preset without
overwriting the original snapshot. Status reports `modified` after owner edits.
A new session enables Ultron again unless you opt out.

To start without Ultron, use `python -m hinata --no-ultron` (or
`python -m hinata --no-ultron gui`). To disable it by default, add this setting
to the existing `[agentic]` section of your `config.toml`:

```toml
[agentic]
ultron_enabled = false
```

The explicit `--ultron` flag overrides that saved opt-out for one session.
Both flags must appear **before** the subcommand. The on/off commands are terminal
controls, not an additional desktop widget. Default Ultron does not start the
scheduler watcher or grant additional permissions to saved jobs.

### What changes

- **Reasoning:** selects `ultra`, using the existing critique, revision, verification,
  second-opinion and reflection machinery. Existing time/model-call limits remain
  unchanged. If thinking is disabled in config or `--no-llm` is used, the status
  explicitly reports deliberation unavailable. Model installation/readiness is a
  separate requirement; this status is not a live model connectivity test.
- **Automation:** selects `LOW_RISK_AUTONOMY`, or `OFFLINE_AUTONOMY` when strict
  offline is configured. Validated low-risk read-only plans can proceed without
  whole-plan approval. Single commands retain the existing permission policy;
  some low-impact mutations (such as creating a folder) are already safe-tier.
- **Authority:** unchanged. Confirmation/PIN gates, revoked tools, lockdown,
  workspace restrictions and network policy still apply. Existing explicit
  Commander Leases retain their existing behavior; Ultron does not create one.
- **Recovery:** revised plans still require fresh approval. Uncertain outcomes and
  human verification requests are not treated as completed objectives.

## Automatic recurring work

Create a job with an explicit automation policy, then keep a watcher running:

```sh
python -m hinata schedule --add "System check" --request "system info" --interval 300 --max-runs 10 --autonomy LOW_RISK_AUTONOMY
python -m hinata schedule --watch
```

The first run is due immediately; subsequent successful runs occur no sooner than
the configured interval from the tick that dispatched them. The watcher polls
every five seconds by default (`--poll-seconds 1..300`). It runs in the foreground,
not as an installed OS service. **Ctrl+C stops it.** A machine must be awake and the
process running for jobs to execute. Sequential dispatch means a long job can delay
other due jobs; model/tool limits still come from the existing configuration.

```sh
python -m hinata schedule                     # inspect saved jobs and last outcomes
python -m hinata schedule --tick              # process one bounded batch, then exit
python -m hinata schedule --disable JOB_ID    # stop future runs
```

Safety defaults and operational details:

- Omit `--autonomy` and the job remains `SUGGEST_ONLY`. Even an Ultron watcher
  **does not upgrade** existing saved jobs. Read the recorded `autonomy_mode`.
- Use `--requires-online` to defer a job while online mode is disabled or strict
  offline is active. This flag does not enable network access or grant permissions.
- Each job is capped at 1–100 attempts. A tick considers at most ten due jobs.
- Jobs without explicit expiry expire 30 days after their first scheduled time.
- Recurrence is fixed-delay from completion. Missed intervals are not replayed.
- Oversized requests are rejected, not truncated. Saved budget overrides can
  lower request limits but cannot raise the assistant configuration limits.
- Attempt history is stored separately. Use `schedule --id JOB_ID` or
  `schedule --offset 50` to inspect parked jobs and previous attempts.
- A runner deadline parks the job as `unknown_outcome`; it does not kill a Python
  thread. This watcher refuses further dispatch while its timed-out worker runs.
- With an enabled vault, new request text is envelope-encrypted. Without it,
  detected secret-like requests are rejected. Old plaintext rows are not migrated.
- A database compare-and-swap reserves and counts each attempt **before** dispatch.
  Competing watchers cannot dispatch the same reserved attempt twice.
- Only successful `completed` results from the assistant renew recurring jobs. Approval, PIN, verification,
  failed, malformed, blocked, cancelled and unknown outcomes disable the job for
  owner review rather than repeatedly attempting it. The last task ID is retained
  when the response supplies one. Inspect it with `python -m hinata tasks TASK_ID`.
- A process crash/interruption during dispatch leaves `enabled = 0` and
  `last_status = running`. This means **possibly executed, outcome unknown**, not
  successful and not automatically retryable. Inspect actual effects and task
  checkpoints before explicitly creating a replacement job. No crash-replay button
  is provided.
- Disabling a job during execution does not undo an already-started action, but a
  late completion cannot re-enable the job or erase its cancelled status.
- Scheduled execution never answers an approval prompt on your behalf. A parked
  single-command request may need to be reissued interactively for approval.
- `completed` is the existing orchestrator's status, not a new universal proof
  that every real-world objective succeeded.

For a model-free smoke test:

```sh
python -m hinata --no-llm --no-speak --ultron ask "system info"
```

## Limits and validation

This is stronger orchestration, not foundation-model training or unlimited
self-improvement. Answer quality depends on your installed model, available RAM,
context, and tools. No OS service, arbitrary shell executor, automatic revised-plan
approval, or unattended multi-day coding loop is introduced.

`tests/test_ultron_automation.py` covers profile restoration, permission boundaries,
saved-policy isolation, bounded recurrence, concurrent workers, cancellation races,
crash/no-replay behavior, validation, and the foreground watcher. These portable
tests do not validate Windows hardware, speech devices, or real-model quality.


## Remediation status

See [the 100-finding tracker](ULTRON_REMEDIATION.md). The original audit and probe
JSON remain historical evidence, not a description of the fixed tree. Counters
and cooperative deadlines do **not** provide a hard memory cap or an OS sandbox.
A model-readiness status of `not_checked` is not evidence that Ollama is ready.
Sensitive approvals still require fresh owner action; redacted schedule review
artifacts do not authorize replay. If shutdown finds an unresolved worker, policy
guards/resources are deliberately retained; the worker is not claimed to be killed.
