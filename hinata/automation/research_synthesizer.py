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


class ResearchSynthesizer:
    def __init__(self, web_search: WebSearchEngine | None = None, llm=None) -> None:
        self.web_search = web_search or WebSearchEngine()
        self.llm = llm

    def _make_queries(self, topic: str) -> list[str]:
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
                        # Deduplicate by URL
                        if not any(x["url"] == r["url"] for x in all_results):
                            all_results.append(r)
                except Exception as exc:
                    errors[q] = str(exc)[:200]

        all_results = all_results[:max_sources]
        facts = []
        for i, r in enumerate(all_results, 1):
            facts.append(f"[{i}] {r['title']} — {r['url']}\n{r.get('snippet','')[:500]}")
        sources_block = "\n\n".join(facts) if facts else "No live sources found; using local knowledge fallback."
        answer = ""
        if self.llm and getattr(self.llm, "config", None) and getattr(self.llm.config, "enabled", False):
            try:
                prompt = (
                    "You are HINATA, a careful research assistant. Synthesize a thorough, balanced answer "
                    "using ONLY the sources below. Cite every factual claim with [N] matching the source number. "
                    "If sources conflict, say so. Distinguish verified facts from unknowns. Be concise but complete.\n\n"
                    f"Topic: {topic}\n\nSources:\n{sources_block}"
                )
                answer = self.llm.chat(
                    [{"role": "user", "content": prompt}],
                    "You synthesize research from verified sources with citations.",
                    connect=True, pull_missing=False, num_predict=1200, role="chat",
                ).strip()
            except Exception:
                answer = ""
        if not answer:
            # Fallback: simple concatenation with instructions
            answer = f"Research synthesis for '{topic}' (offline fallback):\n\n" + "\n\n".join(facts[:8]) if facts else "No verified sources retrieved. Try 'search in browser' or check internet."

        source_lines = "\n".join(f"[{i}] {r['title']} — {r['url']} ({r.get('source','')})" for i, r in enumerate(all_results, 1))
        return {
            "topic": topic,
            "queries": queries,
            "results_count": len(all_results),
            "errors": errors,
            "answer": answer,
            "sources": source_lines,
            "raw_results": all_results,
        }

    def compare_topics(self, topic_a: str, topic_b: str) -> dict[str, Any]:
        a = self.deep_research(topic_a, max_results_per_query=4, max_sources=8)
        b = self.deep_research(topic_b, max_results_per_query=4, max_sources=8)
        # Synthesize comparison if LLM available
        comparison = ""
        if self.llm and getattr(self.llm.config, "enabled", False):
            try:
                prompt = (
                    f"Compare '{topic_a}' vs '{topic_b}' using the evidence below. Be fair, cite sources like [A1], [B1].\n\n"
                    f"A sources:\n" + "\n".join(f"[A{i}] {r['title']} — {r['snippet'][:300]}" for i, r in enumerate(a["raw_results"][:6], 1)) +
                    f"\n\nB sources:\n" + "\n".join(f"[B{i}] {r['title']} — {r['snippet'][:300]}" for i, r in enumerate(b["raw_results"][:6], 1))
                )
                comparison = self.llm.chat([{"role": "user", "content": prompt}], "You compare two topics fairly with citations.").strip()
            except Exception:
                pass
        if not comparison:
            comparison = f"Comparison (fallback): {topic_a} has {a['results_count']} sources; {topic_b} has {b['results_count']} sources. See individual research above."
        return {"topic_a": a, "topic_b": b, "comparison": comparison}

    def summarize_url(self, url: str, *, max_chars: int = 4000) -> dict[str, Any]:
        data = self.web_search.fetch_webpage(url, max_chars=max_chars)
        content = data.get("content", "")
        summary = ""
        if self.llm and getattr(self.llm.config, "enabled", False):
            try:
                summary = self.llm.chat(
                    [{"role": "user", "content": f"Summarize this webpage concisely with key bullet points:\n\nTitle: {data.get('title','')}\nURL: {url}\n\nContent:\n{content[:6000]}"}],
                    "You summarize webpages into clear bullets.",
                ).strip()
            except Exception:
                pass
        if not summary:
            # Naive extractive: first sentences
            sentences = re.split(r"(?<=[.!?])\s+", content)
            summary = " ".join(sentences[:6])[:800]
        return {"url": url, "title": data.get("title",""), "summary": summary, "chars": len(content)}
