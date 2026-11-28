"""Opt-in evaluation against PRELOADED local Ollama models; no downloads/tools.

Example: python scripts/evaluate_ultron_local.py --model YOUR_INSTALLED_MODEL
Use --list-cases to inspect the corpus without any model/network request.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hinata.config import OllamaConfig  # noqa: E402
from hinata.core.errors import BudgetTracker, TaskBudget  # noqa: E402
from hinata.core.llm_manager import LLMManager  # noqa: E402
from hinata.core.reasoning import ThinkingEngine  # noqa: E402
from hinata.core.runtime_limits import CURRENT_BUDGET  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Exact already-installed chat model name")
    parser.add_argument("--host", default="http://127.0.0.1:11434")
    parser.add_argument("--corpus", type=Path, default=ROOT / "evals/ultron-local-v1.json")
    parser.add_argument("--output", type=Path, default=ROOT / "eval-results/ultron-local.json")
    parser.add_argument("--repeat", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--list-cases", action="store_true")
    args = parser.parse_args()
    raw = args.corpus.read_bytes()
    corpus = json.loads(raw)
    if args.list_cases:
        print(json.dumps(corpus, indent=2, ensure_ascii=False))
        return 0
    if not args.model:
        parser.error("--model is required; this tool never selects/downloads a model for you")
    config = OllamaConfig(host=args.host, model=args.model, reviewer_model=args.model,
                          auto_start=False, auto_pull=False, temperature=0, max_tokens=512)
    config.validate()
    llm = LLMManager(config)
    installed = llm.list_models()
    expected = args.model if ":" in args.model else args.model + ":latest"
    if not any(item.get("name") in {args.model, expected} for item in installed):
        parser.error("Requested model is not installed; prepare it separately and rerun")
    # Explicitly disable connect/retry auto-start inside ThinkingEngine's adapter too.
    class LocalOnly:
        def chat(self, *positional: Any, **kwargs: Any) -> str:
            kwargs.update(connect=False, pull_missing=False)
            return llm.chat(*positional, **kwargs)
    adapter = LocalOnly()
    results = []
    for repeat in range(args.repeat):
        for index, case in enumerate(corpus["cases"]):
            modes = ("off", "ultra") if (repeat + index) % 2 == 0 else ("ultra", "off")
            for mode in modes:
                budget = BudgetTracker(TaskBudget(max_model_calls=10, max_tokens=32768))
                token = CURRENT_BUDGET.set(budget)
                started = time.monotonic()
                record: dict[str, Any] = {"case": case["id"], "mode": mode, "repeat": repeat}
                try:
                    engine = ThinkingEngine(adapter, mode=mode, max_model_calls=7, max_seconds=150)
                    result = engine.think(case["prompt"], budget=budget,
                        draft_fn=lambda prompt=case["prompt"]: adapter.chat(
                            [{"role": "user", "content": prompt}],
                            "Answer the user accurately. Obey the requested output format. Never claim tool actions.",
                            num_predict=512))
                    record.update(answer=result.text, passed=bool(re.fullmatch(case["answer_pattern"], result.text.strip())),
                                  trace=result.trace.to_dict(), model_identity=llm.last_model_identity)
                except Exception as exc:
                    record.update(passed=False, error=type(exc).__name__ + ": " + str(exc)[:300])
                finally:
                    CURRENT_BUDGET.reset(token)
                record.update(seconds=round(time.monotonic() - started, 3), usage=budget.snapshot())
                results.append(record)
                report = {"corpus": corpus["version"], "corpus_sha256": hashlib.sha256(raw).hexdigest(),
                          "platform": platform.platform(), "python": platform.python_version(),
                          "installed_models": installed, "results": results,
                          "note": "Exact-answer rubric, counterbalanced cold/warm order; no general quality claim. Token use is estimated; model RSS not measured."}
                args.output.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.output.with_suffix(args.output.suffix + ".tmp")
                temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
                temporary.replace(args.output)
    for mode in ("off", "ultra"):
        subset = [row for row in results if row["mode"] == mode]
        print(f"{mode}: {sum(row['passed'] for row in subset)}/{len(subset)} exact matches; median {statistics.median(row['seconds'] for row in subset):.2f}s")
    print(f"Evidence: {args.output}")
    return 1 if any("error" in row for row in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
