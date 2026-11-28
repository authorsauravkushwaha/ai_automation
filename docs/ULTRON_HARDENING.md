# Additional Ultron safeguards

Date: 2026-11-27. This follow-up implements the five proposed workstreams while
preserving default-on Ultron, saved policy precedence, and confirmation/PIN gates.
It does **not** close every finding in the original 100-item audit.

## 1. Network and browser boundaries

**Public HTTP:** `security/net_guards.py` now resolves DNS at connection time,
rejects an entire answer containing non-public addresses, and connects directly to
one of the validated numeric addresses. There is no second DNS lookup between
validation and socket connection. HTTPS retains the original hostname for SNI and
certificate verification. Ambient proxies remain disabled; redirect targets pass
policy checks and the same connection boundary.

Explicit loopback development URLs use a separate no-redirect client. This is not
an exception available to a redirect from a public URL. OS DNS resolution can still
outlive an HTTP timeout; these changes do not supply an OS firewall.

**Automated Playwright sessions:** HTTP requests are fulfilled by the guarded Python
client rather than `route.continue_()`/`route.fetch()`. The policy is **same origin**,
including scheme and port. It covers navigations, redirects, subresources, and form
requests in the browser context. Service workers, WebSockets, and downloads are
blocked. Chromium's fallback HTTP proxy is deliberately unusable, with background
networking/QUIC and non-proxied WebRTC UDP restricted as defense in depth.

Limits per session: 100 HTTP requests, 16 MB total response bytes, 4 MB per response,
1 MB request bodies, and a cooperative 60-second network window. Response reads
also check a per-response deadline. These controls are not a guarantee against
all native Chromium traffic or a compromised OS; use an OS firewall/namespace for
that threat model.

**Compatibility changes:**
- Cross-origin CDNs, federated login, cross-origin form submissions, streaming and
  WebSocket-heavy sites may not work. The code does not silently weaken policy.
- Selenium automation refuses execution until an equivalent guarded backend exists.
- Explicit OS-browser launches and manually pasted snippets are labeled **not
  guarded**. They are not converted into isolated browser sessions.
- `submitted` remains evidence of a dispatched click, not server acceptance;
  `submission_verified` remains false without an application-specific receipt.

## 2. Subprocess supervision

Workspace developer commands use a trusted absolute-path bootstrap under Python
`-I -S`, before any requested project command runs:

- POSIX: address-space, CPU and file-descriptor limits are installed before `exec`.
  Linux also applies parent-side `prlimit` during startup. No threaded `preexec_fn`.
- Windows: the bootstrap waits for assignment to a configured Job Object before
  releasing the command. Job creation/assignment failure refuses execution rather
  than silently degrading to an unbounded child. The job caps aggregate memory and
  active processes and terminates its tree on close.
- Concurrent pipe draining enforces a combined output quota (default 1 MB), kills
  on overflow, and uses bounded waits after timeout/termination. A dead leader with
  descendants retaining pipes cannot trap the caller in `communicate()` forever.
- Developer Git capture uses the same bounded output collector instead of capturing
  unlimited output before truncation.

**Limits:** POSIX memory limits are per process, not an aggregate cgroup cap. An
escaped POSIX session requires stronger cgroup/container isolation. Git's generic
collector does not add the workspace Job Object/memory policy. General in-process
Python tools still use cooperative thread deadlines; they are not forcibly killed.
This is not a conversion of all tools into isolated workers or a complete filesystem
sandbox. Native network isolation still depends on the reported backend.

## 3. AES-GCM storage and backwards-compatible migration

Install the extra in the Python environment that runs HINATA:

```sh
python -m pip install -e ".[secure-storage]"
```

New envelope writes use **AES-256-GCM from cryptography 48.0.1**, with randomized
salt/nonce, PBKDF2-HMAC-SHA256 key derivation (600,000 iterations), and authenticated
purpose/context. The version is `enc:v2:`. Wrong keys, wrong contexts, tampering,
and unknown versions fail closed. If the extra is missing, new encryption fails
with installation guidance—there is no fallback to the custom v1 writer.

Legacy `enc:v1:` data remains readable without that dependency. Authenticated legacy
values explicitly passed through `protect_text` are upgraded to v2. A missing key
is never silently replaced during decryption. Encrypted JSON exports also write v2
and use unique atomic temporary files; old backups remain readable.

### Backup-first schedule migration

The envelope integration currently covers **schedule request text and explicit
JSON exports**, not all SQLite columns. Do not assume `encrypt_at_rest=true` means
conversations or identities are field-encrypted. Full-database SQLCipher remains a
separate optional backend. This migration accepts ordinary SQLite, not SQLCipher.

1. Stop HINATA and all watchers.
2. Back up the existing `vault.key` securely. On Windows it is DPAPI-bound to its
   owner; copying the database alone is not a recovery plan.
3. Use a new backup filename in an owner-private directory (especially on Windows,
   where inherited directory ACLs matter). The SQLite backup may contain plaintext
   or legacy encrypted data. Treat it as sensitive.
4. Run, substituting your actual paths:

```sh
python scripts/migrate_schedule_vault.py --database PATH_TO_DATABASE --data-dir EXISTING_DATA_DIRECTORY --backup NEW_PRIVATE_BACKUP_PATH --confirm
```

Add `--encrypt-plaintext` only when you also want existing plaintext schedule
requests encrypted. The command requires the **existing** vault key; it never
rotates or generates a replacement key. A plaintext-only installation must deliberately initialize its vault first (for
example, by saving a new encrypted schedule after enabling the vault), then stop
the assistant and back up that key. Never initialize a replacement key for existing
legacy envelopes.
The utility holds a write reservation, takes a consistent SQLite snapshot, upgrades
and verifies fields, then commits together. A corrupt row rolls back all updates;
the backup remains available. Repeated migration is idempotent, and an existing
backup path is never overwritten.

Recovery: stop all processes before restoring the snapshot together with the
matching original key. Preserve the current files for diagnosis; do not restore a
backup over a live WAL database. No automatic restore/key rotation is performed.
The library choice is not a substitute for external cryptographic review.

## 4. Independent filesystem verification

- Writes and binary copies compare complete bounded SHA-256 byte digests, not file
  existence, text prefixes, or decoded text that may hide byte differences.
- Moves capture a source digest before moving and require matching destination
  bytes plus source absence. Without captured evidence (for example after restart)
  the reviewer remains **unknown** rather than guessing. Directory moves likewise
  do not earn unsupported digest verification.
- Archives capture entry names, sizes and digests from the actual bytes archived.
  The reviewer checks the ZIP's complete entry set, lengths, CRC reads and digests
  with entry/expansion limits. Extra, missing, duplicate or changed entries cannot
  pass. Failed construction does not publish a partial destination.
- Patch apply/revert checks fresh workspace content against the recorded after/before
  digests, including disappearance of newly created files on revert. Database status
  alone is no longer proof. Checks enforce approved workspace/root containment and
  byte/file limits. Patch text uses the manager's universal-newline contract for
  Windows compatibility.

Captured file evidence is threaded through plan verification; missing evidence is
not synthesized. General objective completion still requires typed criteria or an
owner review. Parent-directory replacement races still need OS descriptor-relative
hardening; these checks do not claim universal race-free containment.

## 5. Validation and reproducible local evaluation

### Portable regression coverage

`tests/test_ultron_hardening.py` adds 29 tests covering pinned connections/TLS,
browser request mediation, subprocess output/deadline/memory limits, legacy reads,
AES-GCM authentication, transactional migration rollback, and filesystem evidence.
Final local runs: **743 discovered / 687 passed / 56 skipped with cryptography**,
and **743 discovered / 679 passed / 64 skipped with the core alone**. Ruff, Mypy
(92 modules), compilation and diff checks pass.
The crypto cases are exercised with `.[secure-storage]`; core-only environments
explicitly test fail-closed behavior and skip optional crypto integration tests.

CI adds Linux/Windows hardening jobs and a real headless Chromium job using **only
local synthetic fixture servers during the test**. Existing interpreter and GUI
jobs remain. Those remote jobs have not been run/verified in this sandbox.

### Actual Chromium (opt-in, preinstalled browser)

Prepare Playwright and its Chromium binary separately, then run:

```sh
# POSIX
HINATA_REAL_BROWSER_ACCEPTANCE=1 python -m unittest tests.test_real_guarded_browser -v
```

```powershell
# PowerShell
$env:HINATA_REAL_BROWSER_ACCEPTANCE = '1'
python -m unittest tests.test_real_guarded_browser -v
```

The test checks same-origin form delivery and blocks cross-origin redirects and
subresources against two local servers. It never downloads at test runtime and
never submits real user data.

### Real-model comparison (no auto-start or downloads)

Inspect the versioned eight-case corpus without a model:

```sh
python scripts/evaluate_ultron_local.py --list-cases
```

With Ollama already running and the exact model already installed:

```sh
python scripts/evaluate_ultron_local.py --model YOUR_INSTALLED_MODEL --repeat 2
```

The harness compares off/ultra in counterbalanced order, records model metadata,
corpus hash, responses, exact-answer results, latency, trace and estimated budget
usage. It does not execute model-proposed tools, download models, or start Ollama.
Output goes to ignored `eval-results/`. The small exact-answer rubric measures these
specific cases and format compliance—not general intelligence. There is no claim
of better accuracy, performance, GPU memory, or total RSS without real measurements.

### Latest actual-platform validation

The initially missing GUI/browser dependencies were installed for validation.
**All 33 actual Qt offscreen tests and the actual Chromium acceptance test pass.**
The combined suite ran **745 tests: 740 passed, 5 skipped (68.894s)**. The five skips
are optional PDF extraction, the separately opted-in namespace/two-model acceptance,
and three Windows shell-execution tests. This is real Qt widget construction and
real Chromium HTTP/form behavior—not mocked GUI/browser objects. The test-only
GL/DBus loader shims do not validate GPU/physical display behavior.

Chromium 158.0.8127.0 was obtained via `@sparticuz/chromium@158.0.0` from the allowed
npm registry; Playwright 1.65.2 drove it with the production guard arguments. No
`--disable-web-security` or site-isolation bypass flags were used. For a separately
installed binary, the opt-in test accepts `HINATA_TEST_CHROMIUM_EXECUTABLE`; normal
production browser selection is unchanged.

Actual testing found and fixed a redirect gap: cross-origin `Location` headers
must be checked before fulfilling a redirect because browser redirect hops need
not re-enter the routing callback. Tests retain the unusable native proxy as a
second boundary. GUI expectations were also corrected to preserve disabled inference.

Windows and the real-model evaluation are queued for same-branch CI validation;
they are not yet marked passed. Physical laptop/GPU/audio acceptance remains separate.
See `docs/audits/ultron-platform-validation.json` and the remediation tracker.
