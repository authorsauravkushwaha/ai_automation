# HINATA Thinking Layer — Deliberate Reasoning

> A single-pass model answers. A thinking assistant **checks itself first.**
> This layer adds recon → critique → revision → verification → reflection around
> every answer and every proposed plan — still fully local, still bounded, still
> under your approval.

---

## Why this exists

A 0.5B–7B local model one-shots a reply. That is fast, but it also:

- skips part of a multi-part request,
- states a guess with total confidence,
- quietly misses a constraint you gave earlier,
- occasionally says "I opened it" when nothing ran.

The thinking layer attacks exactly those failure modes by running the model
**more than once, in different roles**, and rejecting changes that make an answer
worse. It is not a bigger model — it is a better *process* around the model you
already run.

---

## The loop

```
REQUEST
   │
   ├─ 1. RECON      restate objective, list constraints, list unknowns
   │
   ├─ 2. DRAFT      the model's first answer  (planner role for actions,
   │                                           chat role for conversation)
   │
   ├─ 3. CRITIQUE   independent reviewer hunts for errors, ignored constraints,
   │                missing steps, unsafe assumptions  → strict JSON findings
   │
   ├─ 4. GATE       deterministic filter: vague/self-referential/unsafe
   │                critique points are discarded, not obeyed
   │
   ├─ 5. REVISE     rewrite using only the accepted fixes
   │
   ├─ 6. VERIFY     deterministic, no model:
   │                  • requirement coverage  (did the answer address the ask?)
   │                  • self-consistency      (did revision diverge from draft?)
   │                  • risk flags            (claimed an action? leaked prompt?)
   │
   ├─ 7. 2ND OPINION  deep/ultra only, when confidence is low
   │
   └─ 8. REFLECT     persist a short lesson for future requests
```

### What can never happen

| Guarantee | Where it is enforced |
|---|---|
| Thinking cannot execute a tool | The engine has no tool access; it only returns text |
| Thinking cannot approve itself | Every plan still goes to `Planner.validate` + `PermissionManager` |
| Thinking cannot see or leak chain-of-thought | Only quotes/counts reach the UI; `<think>` blocks are stripped |
| Thinking cannot weaken safety | Critique severity is capped; revisions claiming actions are rejected |
| Thinking cannot hang a request | Wall-clock, model-call and token ceilings; every stage failure falls back to the draft |
| Thinking cannot leak secrets | Memory/knowledge are redacted before they enter any prompt |

---

## Profiles

| Profile | Passes | Typical latency (local 3B) | Use it when |
|---|---|---|---|
| `off` | 1 | baseline | you want the old fast behaviour |
| `quick` | 2 | +1 call | chat questions, low-end hardware |
| `balanced` *(default)* | 3–4 | +2–3 calls | everyday use |
| `deep` | 4–5 | +3–4 calls | research, debugging, decisions |
| `ultra` | 5–6 | +4–5 calls | high-stakes planning, long documents |

A profile can only **lower** the ceilings you set in `[thinking]`; it can never
raise them. Short requests ("hello", "open notepad") skip deliberation in
`balanced`/`deep` because there is nothing to verify.

Deterministic commands (`system info`, `create folder X`, `open spotify`) **do not
touch the thinking layer at all** — they never hit a model in the first place.

---

## Control surface

### Desktop
- **Header → `Think:` dropdown** — switch profile live.
- **Settings → Thinking tab** — enable/disable, profile, hard time limit, max
  extra model calls, stored lesson count.
- **Agent center → "Thinking & Reflection"** — profile, effective ceilings,
  stored lessons, recent traces.
- After a deliberated reply, a compact badge is appended:
  `Thinking: balanced ×2 passes (4.1s, 3 model calls) · 2 fixes · high confidence`

### CLI
```bash
hinata think status                 # JSON profile, limits, lesson count
hinata think deep                   # this session only
hinata think ultra --remember       # also persist to config.toml
hinata think off                    # back to single-pass
```

### Config (`config.toml`)
```toml
[thinking]
enabled = true
mode = "balanced"          # off | quick | balanced | deep | ultra
max_seconds = 150.0
max_model_calls = 7
memory_budget_chars = 1400
knowledge_budget_chars = 1200
require_min_confidence = false
```

---

## What you actually see

Thinking is **auditable, not chatty**. A reply may carry:

```json
{
  "thinking": {
    "mode": "balanced",
    "engaged": true,
    "confidence": "high",
    "confidence_score": 0.82,
    "model_calls": 3,
    "requirements": {"total": 6, "covered": 6, "missing": []},
    "critique": [
      {"title": "Missing rollback step", "severity": "gap",
       "detail": "Add workspace_revert_patch if the test command fails."}
    ],
    "second_opinion_used": false,
    "reflection_id": "refl_a1b2c3d4e5f6a7b8"
  }
}
```

Every engaged run is also written to the tamper-evident audit chain as a
`reasoning` event, so you can replay *how* an answer was reached without ever
storing the private reasoning text.

---

## Reflection memory

After each deliberated request the engine stores one short, deterministic lesson
(for example *"Self-review found gap issues; revise before presenting."*) in the
local `reasoning_reflections` table. Recent lessons are replayed into the
thinking system prompt, so the assistant stops repeating the same class of
mistake. Lessons are redacted, capped at 400 characters, bounded to 200 rows in
memory, and always replayed as **untrusted data**.

Inspect them:
```bash
hinata think status          # includes lesson count
sqlite3 <data_dir>/hinata.db "select confidence, lesson from reasoning_reflections order by rowid desc limit 10;"
```

---

## Honest limitations

- **More calls means more time.** On a 4 GB laptop `ultra` can add 20–60 s per
  request. Start at `balanced`, drop to `quick` if it feels slow.
- **Same model, better process.** If the model simply does not know a fact, extra
  passes will not invent it correctly. Thinking reduces avoidable errors; it does
  not add knowledge.
- **Critique quality is bounded.** The reviewer is the same local model in a
  stricter role. This is why every critique point passes a deterministic gate
  before it is allowed to change an answer.
- **Confidence is a heuristic**, computed from requirement coverage,
  self-consistency and risk flags — not a calibrated probability. Treat `low` as
  "verify this yourself", not as a precise number.
- **No chain-of-thought exposure.** You get structure (what was fixed, how sure it
  is), not the model's private reasoning text. That is deliberate for both safety
  and prompt-injection resistance.

---

## Developer notes

Module: `hinata/core/reasoning.py`

```python
from hinata.core.reasoning import ThinkingEngine, ThinkingContext

engine = ThinkingEngine(llm, mode="deep", max_seconds=90, max_model_calls=6)
result = engine.think(
    "Summarize these three documents and flag contradictions",
    draft_fn=lambda: llm.chat([...], system, role="chat"),
    context=ThinkingContext(memory="...", knowledge="..."),
)
result.text        # final answer (draft if thinking could not improve it)
result.trace       # ReasoningTrace metadata, JSON-serialisable via .to_dict()
```

Integration points:
- `Orchestrator.process()` — chooses `chat` vs `plan` domain from the draft's
  envelope (`draft_kind`), then runs the loop.
- `Orchestrator._thinking_context()` — owns redaction and bounds for evidence.
- `HinataAssistant` — constructs the engine from `[thinking]` config and exposes
  `set_thinking_mode()` / `thinking_status()`.

Tests: `tests/test_reasoning_engine.py` (33 tests, fake model, no network).
