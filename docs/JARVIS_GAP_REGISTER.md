# HINATA J.A.R.V.I.S. capability matrix and gap register

**Audit basis:** working tree on branch `arena/cb618032-ai-automation`, compared with
base commit `ea3eb9b505df742b382010a67dfa98b5116ad068` (audited on 2026-11-15 UTC).
The implementation and tests listed here include the current uncommitted working-tree
changes; no commit or hardware acceptance is implied.

This register is intentionally maintained as machine-readable JSON inside this Markdown file. It records what the code actually proves; a manifest, UI panel, mock, or class name is not treated as operational evidence.

## Capability matrix

```json
{
  "audited_base_commit": "ea3eb9b505df742b382010a67dfa98b5116ad068",
  "working_tree_branch": "arena/cb618032-ai-automation",
  "working_tree_changes_included": true,
  "audited_on": "2026-11-15",
  "states": [
    "IMPLEMENTED_AND_TESTED",
    "IMPLEMENTED_NOT_HARDWARE_VALIDATED",
    "PARTIAL",
    "NOT_IMPLEMENTED"
  ],
  "capabilities": [
    {
      "id": "CAP-AGENT-RUNTIME",
      "request_path": "planner -> orchestrator -> AgentRegistry -> AgentExecutor -> central permission manager -> tool worker -> observation -> persisted report",
      "state": "IMPLEMENTED_AND_TESTED",
      "evidence": ["hinata/agents/runtime.py", "hinata/agents/registry.py", "hinata/core/orchestrator.py", "tests/test_agentic_runtime.py"],
      "validation": ["strict mocked model report", "tool-scope rejection", "report persistence", "delegation cycle/depth/lock checks"],
      "limitation": "The model-backed preflight is bounded and is not yet a multi-iteration specialist work loop."
    },
    {
      "id": "CAP-OBJECTIVE-CONTRACT",
      "request_path": "natural-language request -> Planner.understand_request/build_contract -> persisted contract_json -> plan preview",
      "state": "IMPLEMENTED_AND_TESTED",
      "evidence": ["hinata/core/planner.py", "hinata/tasks/store.py"],
      "validation": ["contract fields are serialized with tasks", "confirmed/inferred/observed fields remain distinct"],
      "limitation": "Ambiguity detection is conservative heuristics; a full interactive clarification dialogue is not implemented."
    },
    {
      "id": "CAP-DURABLE-TASK-LIFECYCLE",
      "request_path": "plan -> TaskStore lease/checkpoints -> observations/effect states -> verification -> UI/CLI report",
      "state": "PARTIAL",
      "evidence": ["hinata/tasks/store.py", "hinata/database/migrations.py", "tests/test_task_checkpoints.py"],
      "validation": ["atomic claim", "crash uncertainty", "idempotency ledger", "pause/resume/reconciliation tests"],
      "limitation": "All requested lifecycle states exist as policy vocabulary, but scheduler-driven restart/resume and full compensation are not complete."
    },
    {
      "id": "CAP-TRUTHFUL-PROJECT-VERIFICATION",
      "request_path": "build_project -> isolated test discovery -> evidence-bearing result -> ActionResult/status -> task record",
      "state": "IMPLEMENTED_AND_TESTED",
      "evidence": ["hinata/core/jarvis_models.py", "hinata/core/orchestrator.py", "tests/test_jarvis_models.py"],
      "validation": ["passing tests", "zero-test suite", "missing verification", "controlled failing test", "deterministic scaffold remains PARTIAL"],
      "limitation": "The builder still has two initial templates and is not yet the iterative software factory described below."
    },
    {
      "id": "CAP-PERSISTENT-PROJECT-REGISTRY",
      "request_path": "project build -> project_records/project_iterations SQLite migrations -> project status and checks",
      "state": "PARTIAL",
      "evidence": ["hinata/database/migrations.py", "hinata/core/jarvis_models.py", "tests/test_jarvis_models.py"],
      "validation": ["status and verification records persist in SQLite"],
      "limitation": "Requirements, architecture decisions, dependency inventory, source indexing refresh, and resume UI are not complete."
    },
    {
      "id": "CAP-INCREMENTAL-SOFTWARE-FACTORY",
      "request_path": "discover/specify/architect/plan/implement/build/test/debug/review/package",
      "state": "PARTIAL",
      "evidence": ["hinata/automation/workspace.py", "hinata/core/jarvis_models.py"],
      "validation": ["bounded patch staging", "pre-image hashes", "rollback", "offline sandbox"],
      "limitation": "Initial model generation remains one bounded response; iterative patch/repair/review loops and broad language adapters are not complete."
    },
    {
      "id": "CAP-MODEL-FLEET-ROUTING",
      "request_path": "hardware profile/model inventory -> role router -> local Ollama request",
      "state": "PARTIAL",
      "evidence": ["hinata/core/providers.py", "hinata/core/llm_manager.py", "tests/test_agentic_runtime.py"],
      "validation": ["role selection", "strict pin/no silent fallback", "hardware profile unit tests"],
      "limitation": "Digests and measured latency/throughput are not persisted as a benchmark history; no real-model result is retained in this audit."
    },
    {
      "id": "CAP-PACKAGE-RELEASE-SUPPLY-CHAIN",
      "request_path": "verified project -> reproducible package -> signed artifact -> owner-approved publish/deploy",
      "state": "NOT_IMPLEMENTED",
      "evidence": ["docs/STAGE_STATUS.md", "hinata/core/jarvis_models.py"],
      "validation": ["No package, signing, publish, or deploy path is exposed by the project factory."],
      "limitation": "The current build path stops at bounded local generation and offline checks; it does not package, sign, publish, or deploy artifacts."
    },
    {
      "id": "CAP-VOICE-VISION-DESKTOP",
      "request_path": "approved microphone/screenshot -> local adapter -> policy-controlled action -> visible state",
      "state": "IMPLEMENTED_NOT_HARDWARE_VALIDATED",
      "evidence": ["hinata/speech/", "hinata/speech/multimodal.py", "hinata/automation/", "hinata/interface/"],
      "validation": ["mocked barge-in", "capability gating", "path/window policy tests"],
      "limitation": "Physical microphone, speaker, display, browser accessibility tree, grounded coordinates, and Windows UI Automation remain target-hardware checks."
    },
    {
      "id": "CAP-OFFLINE-BOUNDARY",
      "request_path": "OfflineGuard + child environment + sandbox backend + optional Windows Sandbox configuration",
      "state": "PARTIAL",
      "evidence": ["hinata/security/offline_guard.py", "hinata/automation/workspace.py", "docs/OFFLINE_ISOLATION.md", "tests/test_offline_linux.py"],
      "validation": ["Python socket guard", "sandbox child guard", "loopback allow", "environment sanitization"],
      "limitation": "Python interception is not an OS firewall; Windows Firewall/Sandbox and native-process escape tests are not run in this Linux audit."
    },
    {
      "id": "CAP-DELIBERATE-REASONING",
      "request_path": "model draft -> ThinkingEngine (recon, critique, revise, verify, reflect) -> validated parser/planner -> owner approval",
      "state": "IMPLEMENTED_AND_TESTED",
      "evidence": ["hinata/core/reasoning.py", "hinata/core/orchestrator.py", "tests/test_reasoning_engine.py", "docs/THINKING_LAYER.md"],
      "validation": ["profile gating (off/quick/balanced/deep/ultra)", "critique acceptance gate", "unsafe-claim revision rejection", "cancellation propagation", "model-call and wall-clock ceilings", "reflection redaction and persistence", "plan approval is never bypassed"],
      "limitation": "Critique and revision use the same local model in a stricter role, so quality is bounded by that model; confidence is a deterministic heuristic over requirement coverage, self-consistency and risk flags, not a calibrated probability. No chain-of-thought is exposed. Latency grows with profile."
    },
    {
      "id": "CAP-MEMORY-RAG",
      "request_path": "local documents/conversations -> structured memory/index -> cited retrieval -> redacted persistence",
      "state": "IMPLEMENTED_AND_TESTED",
      "evidence": ["hinata/core/memory.py", "hinata/knowledge/index.py", "tests/test_coding_rag_and_security.py", "tests/test_semantic_knowledge.py"],
      "validation": ["secret blocking", "contradiction handling", "hybrid retrieval", "citation validation", "incremental index tests"],
      "limitation": "Large-corpus benchmarking, OCR for scanned PDFs, and independent security review remain open."
    },
    {
      "id": "CAP-PROACTIVE-AUTOMATION",
      "request_path": "owner-approved trigger -> scheduler -> bounded task -> notification/audit",
      "state": "PARTIAL",
      "evidence": ["hinata/tasks/scheduler.py", "hinata/tasks/playbooks.py", "tests/test_agentic_runtime.py"],
      "validation": ["offline schedule blocking", "run caps", "playbook validation"],
      "limitation": "File/project/system event watchers, leases, cooldown policy, and operational disable UI are incomplete."
    },
    {
      "id": "CAP-OPERATIONAL-CONTROL-CENTER",
      "request_path": "runtime records -> desktop control center panels",
      "state": "PARTIAL",
      "evidence": ["hinata/interface/control_center.py", "hinata/interface/main_window.py"],
      "validation": ["offscreen GUI construction tests"],
      "limitation": "Several requested panels do not yet expose full live DAG, project studio, model benchmark, and evidence data."
    }
  ]
}
```

## Gap register

```json
[
  {
    "id": "GAP-AGENT-001",
    "severity": "medium",
    "affected_files": ["hinata/agents/runtime.py", "hinata/agents/registry.py", "hinata/core/orchestrator.py"],
    "impact": "Model-backed specialist work is currently a strict preflight plus centrally executed bounded actions, not a multi-iteration worker loop.",
    "proposed_repair": "Persist assignment context and observations per iteration; add bounded specialist repair/retry reports and reviewer handoff without granting agents direct side effects.",
    "tests": ["model report schema", "scope bypass", "cancellation", "per-agent budgets", "independent reviewer rejection"],
    "status": "OPEN"
  },
  {
    "id": "GAP-PROJECT-001",
    "severity": "high",
    "affected_files": ["hinata/core/jarvis_models.py", "hinata/automation/workspace.py"],
    "impact": "Project creation is still an initial scaffold/template path; it cannot yet resume a substantial project through discover-to-release iterations.",
    "proposed_repair": "Add a ProjectFactory state machine with bounded file patch proposals, language adapters, focused test/debug/repair cycles, project memory, and owner acceptance checkpoints.",
    "tests": ["multi-iteration resume", "repair convergence", "unrelated Git changes", "package reproducibility", "secret exclusion"],
    "status": "OPEN"
  },
  {
    "id": "GAP-TASK-001",
    "severity": "high",
    "affected_files": ["hinata/tasks/store.py", "hinata/tasks/scheduler.py"],
    "impact": "Lifecycle vocabulary and leases exist, but not every state has a production transition handler or crash-safe scheduler resume path.",
    "proposed_repair": "Implement an explicit transition/event reducer, dependency wait records, task reconciliation UI, non-idempotent compensation registry, and restart integration tests.",
    "tests": ["crash at every checkpoint", "two-instance claim", "unknown outcome", "lease expiry", "cancel propagation"],
    "status": "OPEN"
  },
  {
    "id": "GAP-MODEL-001",
    "severity": "medium",
    "affected_files": ["hinata/core/providers.py", "hinata/core/llm_manager.py", "hinata/core/jarvis_models.py"],
    "impact": "Routing is hardware-aware and role-aware but still relies on best-effort metadata and heuristic capability detection.",
    "proposed_repair": "Persist model digests/capabilities and benchmark cold start, warm latency, throughput, memory, and power before recommending a role.",
    "tests": ["real local model opt-in", "capability mismatch", "benchmark persistence", "strict pin fallback"],
    "status": "OPEN"
  },
  {
    "id": "GAP-SECURITY-001",
    "severity": "critical",
    "affected_files": ["hinata/security/offline_guard.py", "hinata/automation/workspace.py", "docs/OFFLINE_ISOLATION.md"],
    "impact": "The process socket guard and sandbox helpers do not prove an operating-system-wide network boundary for every native child process.",
    "proposed_repair": "Run Windows Firewall/Sandbox isolation acceptance tests, record native child-process evidence, and keep the UI wording scoped to the backend actually active.",
    "tests": ["native child egress", "Windows Sandbox", "Firewall profile", "escape/kill tree", "resource quotas"],
    "status": "OPEN"
  },
  {
    "id": "GAP-HARDWARE-001",
    "severity": "high",
    "affected_files": ["hinata/speech/", "hinata/interface/", "hinata/automation/windows_control.py"],
    "impact": "Physical microphone, speaker, display, and Windows desktop behavior are not validated by portable tests.",
    "proposed_repair": "Run opt-in target Windows acceptance scenarios and retain measurements/logs without replacing mock tests with claims.",
    "tests": ["voice barge-in", "low confidence clarification", "stale screenshot target", "window grounding", "resource budget"],
    "status": "OPEN"
  },
  {
    "id": "GAP-UX-001",
    "severity": "medium",
    "affected_files": ["hinata/interface/control_center.py", "hinata/interface/main_window.py"],
    "impact": "The control center does not yet surface every persisted project, DAG, specialist report, benchmark, and approval evidence record.",
    "proposed_repair": "Bind panels to TaskStore/project_records/agent_subtask_reports and label unavailable hardware/model data NOT_IMPLEMENTED or NOT_RUN.",
    "tests": ["live update throttling", "approval exact parameters", "cancel/pause", "accessibility", "no false completion"],
    "status": "OPEN"
  }
]
```

### State definitions

- `IMPLEMENTED_AND_TESTED`: code path is exercised by automated tests in this repository; it does not imply real-model or physical-device validation.
- `IMPLEMENTED_NOT_HARDWARE_VALIDATED`: an operational adapter exists and portable tests cover policy/readiness, but the target hardware or OS is not validated here.
- `PARTIAL`: a useful bounded subset exists, but the requested end-to-end behavior is not complete.
- `NOT_IMPLEMENTED`: no honest operational path exists yet; UI or metadata alone does not count.
