"""Research Synthesizer — deep multi-query research with synthesis.

Uses existing WebSearchEngine (DuckDuckGo + Wikipedia) to run several queries in parallel,
then synthesizes a cited answer via LLM if available, else concatenates verified excerpts.
"""

from __future__ import annotations

import concurrent.futures
import re
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.automation.web_search import WebSearchEngine
from hinata.security.redaction import redact_text


def _clamp_int(value: object, *, low: int, high: int, default: int) -> int:
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(number, high))


def _llm_enabled(llm: Any) -> bool:
    config = getattr(llm, "config", None) if llm is not None else None
    return bool(config is not None and getattr(config, "enabled", False))


def _redacted_error(exc: Exception) -> str:
    return redact_text(str(exc))[:300]


def _result_text(result: Any, key: str, default: str = "") -> str:
    if isinstance(result, dict):
        return str(result.get(key) or default)
    return default


class ResearchSynthesizer:
    def __init__(self, web_search: WebSearchEngine | None = None, llm: Any = None) -> None:
        self.web_search = web_search or WebSearchEngine()
        self.llm = llm

    def _make_queries(self, topic: str) -> list[str]:
        if not isinstance(topic, str):
            raise AutomationError("Research topic must be a string")
        topic = topic.strip()[:500]
        if not topic:
            raise AutomationError("Research topic cannot be empty")
        base = topic
        return [
            base,
            f"{base} overview",
            f"{base} latest news 2025 2026",
            f"{base} explained",
        ]

    def deep_research(self, topic: str, *, max_results_per_query: int = 5, max_sources: int = 12) -> dict[str, Any]:
        # Clamp here as well as in the action schema: direct library callers
        # (and the intra-process tool path) bypass validation, and a negative
        # max_sources silently becomes a negative slice — ``results[:-5]``
        # keeps all but the last five instead of refusing the request.
        max_results_per_query = _clamp_int(
            max_results_per_query, low=1, high=10, default=5
        )
        max_sources = _clamp_int(max_sources, low=1, high=20, default=12)
        queries = self._make_queries(topic)
        all_results: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
            futs = {pool.submit(self.web_search.search, q, max_results=max_results_per_query): q for q in queries}
            for fut in concurrent.futures.as_completed(futs):
                q = futs[fut]
                try:
                    data = fut.result()
                    for r in data.get("results", []):
                        if not isinstance(r, dict):
                            continue
                        url = str(r.get("url") or "")
                        if url and not any(x.get("url") == url for x in all_results):
                            all_results.append(r)
                except Exception as exc:
                    errors[q] = _redacted_error(exc)

        all_results = all_results[:max_sources]
        facts = []
        for i, r in enumerate(all_results, 1):
            facts.append(
                f"[{i}] {_result_text(r, 'title', 'Untitled source')} — "
                f"{_result_text(r, 'url')}\n{_result_text(r, 'snippet')[:500]}"
            )
        sources_block = "\n\n".join(facts) if facts else "No live sources found; using local knowledge fallback."
        answer = ""
        synthesis_error = ""
        if self.llm and _llm_enabled(self.llm):
            try:
                prompt = (
                    "You are HINATA, a careful research assistant. Synthesize a thorough, balanced answer "
                    "using ONLY the sources below. Cite every factual claim with [N] matching the source number. "
                    "If sources conflict, say so. Distinguish verified facts from unknowns. Be concise but complete.\n\n"
                    f"Topic: {topic}\n\nSources:\n{sources_block}"
                )
                answer = str(
                    self.llm.chat(
                        [{"role": "user", "content": prompt}],
                        "You synthesize research from verified sources with citations.",
                        connect=True, pull_missing=False, num_predict=1200, role="chat",
                    )
                ).strip()
            except Exception as exc:
                synthesis_error = _redacted_error(exc)
        if not answer:
            # Fallback: simple concatenation with instructions
            answer = f"Research synthesis for '{topic}' (offline fallback):\n\n" + "\n\n".join(facts[:8]) if facts else "No verified sources retrieved. Try 'search in browser' or check internet."

        source_lines = "\n".join(
            f"[{i}] {_result_text(r, 'title', 'Untitled source')} — "
            f"{_result_text(r, 'url')} ({_result_text(r, 'source')})"
            for i, r in enumerate(all_results, 1)
        )
        result: dict[str, Any] = {
            "topic": topic,
            "queries": queries,
            "results_count": len(all_results),
            "errors": errors,
            "answer": answer,
            "sources": source_lines,
            "raw_results": all_results,
        }
        if synthesis_error:
            result["synthesis_error"] = synthesis_error
        return result

    def compare_topics(self, topic_a: str, topic_b: str) -> dict[str, Any]:
        a = self.deep_research(topic_a, max_results_per_query=4, max_sources=8)
        b = self.deep_research(topic_b, max_results_per_query=4, max_sources=8)
        # Synthesize comparison if LLM available
        comparison = ""
        comparison_error = ""
        if self.llm and _llm_enabled(self.llm):
            try:
                prompt = (
                    f"Compare '{topic_a}' vs '{topic_b}' using the evidence below. Be fair, cite sources like [A1], [B1].\n\n"
                    "A sources:\n" + "\n".join(
                        f"[A{i}] {_result_text(r, 'title', 'Untitled source')} — {_result_text(r, 'snippet')[:300]}"
                        for i, r in enumerate(a["raw_results"][:6], 1)
                    ) +
                    "\n\nB sources:\n" + "\n".join(
                        f"[B{i}] {_result_text(r, 'title', 'Untitled source')} — {_result_text(r, 'snippet')[:300]}"
                        for i, r in enumerate(b["raw_results"][:6], 1)
                    )
                )
                comparison = str(
                    self.llm.chat([{"role": "user", "content": prompt}], "You compare two topics fairly with citations.")
                ).strip()
            except Exception as exc:
                comparison_error = _redacted_error(exc)
        if not comparison:
            comparison = f"Comparison (fallback): {topic_a} has {a['results_count']} sources; {topic_b} has {b['results_count']} sources. See individual research above."
        result: dict[str, Any] = {
            "topic_a": a,
            "topic_b": b,
            "comparison": comparison,
        }
        if comparison_error:
            result["comparison_error"] = comparison_error
        return result

    def summarize_url(self, url: str, *, max_chars: int = 4000) -> dict[str, Any]:
        if not isinstance(url, str):
            raise AutomationError("URL must be a string")
        cap = _clamp_int(max_chars, low=200, high=15_000, default=4_000)
        data = self.web_search.fetch_webpage(url, max_chars=cap)
        content = str(data.get("content", "") or "")
        summary = ""
        summary_error = ""
        if self.llm and _llm_enabled(self.llm):
            try:
                summary = str(
                    self.llm.chat(
                        [{"role": "user", "content": f"Summarize this webpage concisely with key bullet points:\n\nTitle: {data.get('title','')}\nURL: {url}\n\nContent:\n{content[:6000]}"}],
                        "You summarize webpages into clear bullets.",
                    )
                ).strip()
            except Exception as exc:
                summary_error = _redacted_error(exc)
        if not summary:
            # Naive extractive: first sentences
            sentences = re.split(r"(?<=[.!?])\s+", content)
            summary = " ".join(sentences[:6])[:800]
        result: dict[str, Any] = {
            "url": url,
            "title": data.get("title", ""),
            "summary": summary,
            "chars": len(content),
        }
        if summary_error:
            result["summary_error"] = summary_error
        return result
