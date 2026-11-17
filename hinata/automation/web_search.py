"""Fast, parallel, SSRF-safe internet search and webpage reader for HINATA.

Uses only the Python standard library (``urllib``, ``html.parser``, ``concurrent.futures``)
to query multiple live sources concurrently (DuckDuckGo Web, DuckDuckGo Instant Answer,
Wikipedia, OpenLibrary for book authors/works, and GitHub for software authors/projects),
extract clean page excerpts, and respect HINATA's ``strict_offline`` & ``OfflineGuard`` policies.
"""

from __future__ import annotations

import concurrent.futures
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from hinata.automation.app_control import AutomationError
from hinata.security.offline_guard import OfflineGuard, OfflinePolicyError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) HINATA-Assistant/1.0 Safari/537.36"
)
MAX_DOWNLOAD_BYTES = 260_000


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _ReadableHTMLParser(HTMLParser):
    """Strip scripts/styles/navigation and extract clean human-readable page text."""

    SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "head", "iframe", "template", "nav", "footer"})
    BLOCK_TAGS = frozenset({"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "br", "tr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if lower == "title":
            self._in_title = True
        elif self._skip_depth == 0 and lower in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if lower == "title":
            self._in_title = False
        elif self._skip_depth == 0 and lower in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth > 0:
            return
        cleaned = " ".join(data.split())
        if cleaned:
            self.chunks.append(cleaned + " ")

    def extracted_title(self) -> str:
        return " ".join("".join(self.title_parts).split())[:200]

    def extracted_text(self, max_chars: int = 4000) -> str:
        raw = "".join(self.chunks)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        non_empty = [line for line in lines if len(line) >= 2]
        joined = "\n".join(non_empty)
        return joined[:max_chars].strip()


class _DuckDuckGoHTMLParser(HTMLParser):
    """Extract organic search result titles, decoded URLs, and snippets from DuckDuckGo HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[WebSearchResult] = []
        self._current_url = ""
        self._in_title = False
        self._in_snippet = False
        self._title_buf: list[str] = []
        self._snippet_buf: list[str] = []

    @staticmethod
    def _decode_ddg_href(href: str) -> str:
        if not href:
            return ""
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlsplit(href)
        if "duckduckgo.com" in (parsed.hostname or "") and parsed.path.startswith("/l/"):
            qs = parse_qs(parsed.query)
            if "uddg" in qs and qs["uddg"]:
                return unquote(qs["uddg"][0])
        if parsed.scheme in {"http", "https"} and "duckduckgo.com/y.js" not in href:
            return href
        return ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        cls = attr_map.get("class", "")
        if tag.lower() == "a" and ("result__a" in cls or "result-link" in cls):
            decoded = self._decode_ddg_href(attr_map.get("href", ""))
            if decoded:
                if self._current_url and self._title_buf:
                    self._flush()
                self._current_url = decoded
                self._in_title = True
                self._title_buf = []
                self._snippet_buf = []
        elif tag.lower() in {"a", "td", "div", "span"} and (
            "result__snippet" in cls or "result-snippet" in cls
        ):
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._in_title:
            self._in_title = False
        elif self._in_snippet and tag.lower() in {"a", "td", "div", "span"}:
            self._in_snippet = False
            if self._current_url and self._title_buf:
                self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)
        elif self._in_snippet:
            self._snippet_buf.append(data)

    def _flush(self) -> None:
        title = " ".join("".join(self._title_buf).split()).strip()
        snippet = " ".join("".join(self._snippet_buf).split()).strip()
        url = self._current_url.strip()
        self._current_url = ""
        self._title_buf = []
        self._snippet_buf = []
        if title and url.startswith(("http://", "https://")):
            self.results.append(
                WebSearchResult(
                    title=title[:200],
                    url=url[:600],
                    snippet=snippet[:500],
                    source="web",
                )
            )


def extract_core_search_query(raw_text: str) -> str:
    """Distill a clean search-engine query from a long conversational prompt.

    For example:
    ``"Search about author Saurav Kushwaha, from scratch to last. Tell me his evry wrok..."``
    -> ``"author Saurav Kushwaha"`` while preserving the full user prompt for answer synthesis.
    """
    cleaned = " ".join(raw_text.strip().split())
    cleaned = re.sub(
        r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?"
        r"(?:search\s+(?:from\s+|on\s+)?(?:the\s+)?(?:internet|web|online|google)\s+(?:for|about)\s+"
        r"|(?:web|internet|online|google)\s+search\s+(?:for|about)?\s*"
        r"|search\s+(?:about|for)\s+"
        r"|look\s+up\s+"
        r"|google\s+"
        r"|tell\s+me\s+(?:everything\s+)?about\s+)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # Strip trailing conversational instructions once we have the primary topic clause.
    clause_split = re.split(
        r"(?:[.;!?]\s+(?:tell\s+me|explain|give\s+me|what\s+|list\s+|describe|summarize|include|from\s+start)"
        r"|,\s*from\s+(?:scratch|start|beginning)\b"
        r"|\s+(?:and\s+)?tell\s+me\s+(?:his|her|their|its|every|evry|all)\b)",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    primary = clause_split[0].strip(" ,.;:!?\"'")
    if len(primary) >= 3:
        return primary[:160]
    return cleaned[:160]


def is_safe_public_url(url: str) -> bool:
    """Validate HTTP(S) URL and block SSRF to loopback, private, link-local, or reserved IPs."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.strip("[]").lower()
    if host in {"localhost", "0.0.0.0"} or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        # Fail closed: do not hand an unresolvable hostname to urllib's independent resolver.
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            resolved_ip = ipaddress.ip_address(str(sockaddr[0]).split("%")[0])
        except ValueError:
            continue
        if (
            resolved_ip.is_loopback
            or resolved_ip.is_private
            or resolved_ip.is_link_local
            or resolved_ip.is_multicast
            or resolved_ip.is_reserved
            or resolved_ip.is_unspecified
        ):
            return False
    return True


class _SafePublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow HTTP redirects only to validated public HTTP/HTTPS targets."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        resolved = urljoin(req.full_url, newurl)
        if not is_safe_public_url(resolved):
            raise urllib.error.URLError("Blocked redirect to non-public or unsafe URL")
        return super().redirect_request(req, fp, code, msg, headers, resolved)


class WebSearchEngine:
    """Multi-source concurrent internet search and readable webpage fetcher."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        strict_offline: bool = False,
        offline_guard: OfflineGuard | None = None,
        timeout_seconds: float = 6.0,
    ) -> None:
        self.enabled = enabled
        self.strict_offline = strict_offline
        self.offline_guard = offline_guard
        self.timeout_seconds = max(1.5, min(float(timeout_seconds), 20.0))
        self._opener = urllib.request.build_opener(_SafePublicRedirectHandler())

    def _check_policy(self, url: str) -> None:
        if self.strict_offline:
            raise AutomationError(
                "Internet search is blocked because strict_offline = true is active in config.toml."
            )
        if not self.enabled:
            raise AutomationError(
                "Internet search is currently disabled. Enable web_search_enabled = true in Settings or config.toml."
            )
        if self.offline_guard is not None:
            if self.offline_guard.strict_offline:
                raise AutomationError(
                    "Internet search is blocked by active strict_offline socket guard."
                )
            if self.offline_guard.online_mode_enabled and self.offline_guard.allowed_online_domains:
                try:
                    self.offline_guard.check_url(url, purpose="web_search")
                except OfflinePolicyError as exc:
                    raise AutomationError(str(exc)) from exc
        if not is_safe_public_url(url):
            raise AutomationError(f"Refused non-public or invalid URL: {url}")

    def _http_get(
        self,
        url: str,
        *,
        accept: str = "text/html,application/json;q=0.9,*/*;q=0.8",
        max_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> tuple[bytes, str]:
        self._check_policy(url)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": accept,
                "Accept-Language": "en-US,en;q=0.9",
            },
            method="GET",
        )
        with self._opener.open(req, timeout=self.timeout_seconds) as resp:
            ctype = str(resp.headers.get("Content-Type", ""))
            body = resp.read(max_bytes)
        return body, ctype

    def _search_duckduckgo_html(self, query: str, limit: int = 8) -> list[WebSearchResult]:
        encoded = urllib.parse.urlencode({"q": query})
        raw, _ = self._http_get(f"https://html.duckduckgo.com/html/?{encoded}")
        parser = _DuckDuckGoHTMLParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        if parser._current_url and parser._title_buf:
            parser._flush()
        return parser.results[:limit]

    def _search_duckduckgo_lite(self, query: str, limit: int = 6) -> list[WebSearchResult]:
        """Accessible text-only DuckDuckGo endpoint as an HTML search fallback."""
        encoded = urllib.parse.urlencode({"q": query})
        raw, _ = self._http_get(f"https://lite.duckduckgo.com/lite/?{encoded}")
        parser = _DuckDuckGoHTMLParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        if parser._current_url and parser._title_buf:
            parser._flush()
        return [
            WebSearchResult(item.title, item.url, item.snippet, "web")
            for item in parser.results[:limit]
        ]

    def _search_duckduckgo_instant(self, query: str) -> list[WebSearchResult]:
        encoded = urllib.parse.urlencode(
            {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
        )
        raw, _ = self._http_get(
            f"https://api.duckduckgo.com/?{encoded}", accept="application/json"
        )
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        out: list[WebSearchResult] = []
        abstract = str(payload.get("AbstractText") or "").strip()
        abs_url = str(payload.get("AbstractURL") or "").strip()
        heading = str(payload.get("Heading") or query).strip()
        if abstract and abs_url:
            out.append(
                WebSearchResult(
                    title=heading[:200],
                    url=abs_url[:600],
                    snippet=abstract[:600],
                    source="duckduckgo_instant",
                )
            )
        for topic in payload.get("RelatedTopics", [])[:4]:
            if isinstance(topic, dict) and topic.get("Text") and topic.get("FirstURL"):
                out.append(
                    WebSearchResult(
                        title=str(topic["Text"])[:120],
                        url=str(topic["FirstURL"])[:600],
                        snippet=str(topic["Text"])[:400],
                        source="duckduckgo_instant",
                    )
                )
        return out

    def _search_wikipedia(self, query: str, limit: int = 3) -> list[WebSearchResult]:
        params = urllib.parse.urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": str(max(1, min(limit, 5))),
                "utf8": "1",
                "format": "json",
            }
        )
        raw, _ = self._http_get(
            f"https://en.wikipedia.org/w/api.php?{params}", accept="application/json"
        )
        data = json.loads(raw.decode("utf-8", errors="replace"))
        hits = data.get("query", {}).get("search", [])
        results: list[WebSearchResult] = []
        for item in hits[:limit]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            snippet_html = str(item.get("snippet") or "")
            snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet_html)).strip()
            if title:
                wiki_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
                results.append(
                    WebSearchResult(
                        title=f"{title} — Wikipedia",
                        url=wiki_url,
                        snippet=snippet[:500],
                        source="wikipedia",
                    )
                )
        return results

    def _search_openlibrary(self, query: str, limit: int = 4) -> list[WebSearchResult]:
        # Strip role prefix like "author " so OpenLibrary matches the author/book cleanly
        clean_q = re.sub(r"^\b(?:author|writer|novelist|books?\s+by)\b\s+", "", query, flags=re.IGNORECASE).strip() or query
        params = urllib.parse.urlencode({"q": clean_q, "limit": str(limit)})
        raw, _ = self._http_get(
            f"https://openlibrary.org/search.json?{params}", accept="application/json"
        )
        data = json.loads(raw.decode("utf-8", errors="replace"))
        docs = data.get("docs", [])
        out: list[WebSearchResult] = []
        for doc in docs[:limit]:
            if not isinstance(doc, dict):
                continue
            title = str(doc.get("title") or "").strip()
            authors = ", ".join(str(a) for a in (doc.get("author_name") or [])[:3])
            year = doc.get("first_publish_year")
            key = str(doc.get("key") or "").strip()
            if title and key:
                meta = f"Author(s): {authors}." if authors else ""
                if year:
                    meta += f" First published: {year}."
                subjects = ", ".join(str(s) for s in (doc.get("subject") or [])[:5])
                if subjects:
                    meta += f" Subjects: {subjects}."
                out.append(
                    WebSearchResult(
                        title=f"{title} ({authors or 'Book'})",
                        url=f"https://openlibrary.org{key}",
                        snippet=meta.strip()[:500],
                        source="openlibrary",
                    )
                )
        return out

    def _search_github(self, query: str, limit: int = 4) -> list[WebSearchResult]:
        clean_q = re.sub(r"^\b(?:author|developer|creator|github)\b\s+", "", query, flags=re.IGNORECASE).strip() or query
        compact_handle = re.sub(r"[^a-zA-Z0-9]", "", query).lower()
        out: list[WebSearchResult] = []

        # Search GitHub users matching the name or handle
        user_params = urllib.parse.urlencode({"q": clean_q, "per_page": "3"})
        raw_users, _ = self._http_get(
            f"https://api.github.com/search/users?{user_params}",
            accept="application/vnd.github+json",
        )
        users_data = json.loads(raw_users.decode("utf-8", errors="replace"))
        candidate_logins: list[str] = []
        for item in (users_data.get("items") or [])[:2]:
            if isinstance(item, dict) and item.get("login"):
                candidate_logins.append(str(item["login"]))
        if compact_handle and compact_handle not in {u.lower() for u in candidate_logins} and len(compact_handle) >= 5:
            candidate_logins.insert(0, compact_handle)

        for login in candidate_logins[:2]:
            try:
                u_raw, _ = self._http_get(
                    f"https://api.github.com/users/{urllib.parse.quote(login)}",
                    accept="application/vnd.github+json",
                )
                u_obj = json.loads(u_raw.decode("utf-8", errors="replace"))
                if not isinstance(u_obj, dict) or not u_obj.get("html_url"):
                    continue
                name = str(u_obj.get("name") or login)
                bio = str(u_obj.get("bio") or "").strip()
                pub_repos = u_obj.get("public_repos", 0)
                followers = u_obj.get("followers", 0)
                repos_summary = ""
                try:
                    r_raw, _ = self._http_get(
                        f"https://api.github.com/users/{urllib.parse.quote(login)}/repos?sort=updated&per_page=6",
                        accept="application/vnd.github+json",
                    )
                    r_list = json.loads(r_raw.decode("utf-8", errors="replace"))
                    if isinstance(r_list, list) and r_list:
                        repo_bits = [
                            f"{r.get('name')} ({r.get('language') or 'project'}: {str(r.get('description') or 'repository')[:90]})"
                            for r in r_list[:6]
                            if isinstance(r, dict) and r.get("name")
                        ]
                        if repo_bits:
                            repos_summary = " Works/Repositories: " + "; ".join(repo_bits)
                except Exception:
                    pass
                snippet = (
                    f"GitHub profile @{login} ({name}). {bio} "
                    f"Public repositories: {pub_repos}, Followers: {followers}.{repos_summary}"
                ).strip()
                out.append(
                    WebSearchResult(
                        title=f"{name} (@{login}) — GitHub",
                        url=str(u_obj["html_url"]),
                        snippet=snippet[:700],
                        source="github",
                    )
                )
            except Exception:
                continue

        # Also check matching public repositories
        repo_params = urllib.parse.urlencode({"q": clean_q, "per_page": str(max(1, limit - len(out)))})
        try:
            raw_repos, _ = self._http_get(
                f"https://api.github.com/search/repositories?{repo_params}",
                accept="application/vnd.github+json",
            )
            repos_data = json.loads(raw_repos.decode("utf-8", errors="replace"))
            for repo in (repos_data.get("items") or [])[:2]:
                if not isinstance(repo, dict) or not repo.get("html_url"):
                    continue
                full_name = str(repo.get("full_name") or "")
                desc = str(repo.get("description") or "Public repository").strip()
                lang = str(repo.get("language") or "")
                stars = repo.get("stargazers_count", 0)
                out.append(
                    WebSearchResult(
                        title=f"{full_name} — GitHub Repository",
                        url=str(repo["html_url"]),
                        snippet=f"{desc} (Language: {lang or 'multi'}, Stars: {stars})"[:500],
                        source="github",
                    )
                )
        except Exception:
            pass
        return out[:limit]

    def fetch_webpage(self, url: str, *, max_chars: int = 4000) -> dict[str, Any]:
        """Fetch and extract readable plain text from a public HTTP/HTTPS webpage."""
        cap = max(200, min(int(max_chars), 15_000))
        try:
            raw, ctype = self._http_get(url)
        except AutomationError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise AutomationError(f"Could not fetch webpage '{url}': {exc}") from exc

        lower_ctype = ctype.lower()
        decoded = raw.decode("utf-8", errors="replace")
        if "application/json" in lower_ctype:
            text = decoded[:cap].strip()
            title = url
        else:
            parser = _ReadableHTMLParser()
            parser.feed(decoded)
            title = parser.extracted_title() or url
            text = parser.extracted_text(max_chars=cap)
        return {
            "url": url,
            "title": title,
            "content": text,
            "chars": len(text),
        }

    def search(
        self,
        query: str,
        *,
        max_results: int = 8,
        deep_read: bool = True,
    ) -> dict[str, Any]:
        """Search multiple internet sources concurrently and optionally read top excerpts."""
        if self.strict_offline:
            raise AutomationError(
                "Internet search is blocked because strict_offline = true is active in config.toml."
            )
        if not self.enabled:
            raise AutomationError(
                "Internet search is currently disabled. Enable web_search_enabled = true in Settings or config.toml."
            )
        full_query = " ".join(query.strip().split())
        if not full_query:
            raise AutomationError("Search query must not be empty")
        core_query = extract_core_search_query(full_query)
        cap = max(1, min(int(max_results), 12))

        provider_calls: list[tuple[str, Any]] = [
            ("ddg_html", lambda: self._search_duckduckgo_html(core_query, limit=cap)),
            ("ddg_lite", lambda: self._search_duckduckgo_lite(core_query, limit=cap)),
            ("ddg_instant", lambda: self._search_duckduckgo_instant(core_query)),
            ("wikipedia", lambda: self._search_wikipedia(core_query, limit=3)),
            ("github", lambda: self._search_github(core_query, limit=3)),
        ]
        lower_q = full_query.lower()
        if any(w in lower_q for w in ("author", "book", "novel", "writer", "work", "wrok", "publish", "poem", "story")):
            provider_calls.append(("openlibrary", lambda: self._search_openlibrary(core_query, limit=4)))

        gathered: list[WebSearchResult] = []
        provider_errors: dict[str, str] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(provider_calls), thread_name_prefix="hinata-web"
        ) as pool:
            future_map = {pool.submit(fn): name for name, fn in provider_calls}
            for fut in concurrent.futures.as_completed(future_map):
                name = future_map[fut]
                try:
                    items = fut.result()
                    if items:
                        gathered.extend(items)
                except Exception as exc:
                    provider_errors[name] = str(exc)[:200]

        # Make citations deterministic and prioritize broad web hits before specialized APIs.
        source_rank = {"web": 0, "wikipedia": 1, "openlibrary": 2, "duckduckgo_instant": 3, "github": 4}
        gathered.sort(key=lambda item: source_rank.get(item.source, 9))

        # Deduplicate preserving quality ordering (organic web + verified APIs)
        deduped: list[WebSearchResult] = []
        seen_urls: set[str] = set()
        for item in gathered:
            norm_url = item.url.rstrip("/").lower()
            if norm_url in seen_urls:
                continue
            seen_urls.add(norm_url)
            deduped.append(item)
            if len(deduped) >= cap:
                break

        page_excerpts: list[dict[str, str]] = []
        if deep_read and deduped:
            targets = [r for r in deduped[:3] if r.source in {"web", "wikipedia"}]
            if targets:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(3, len(targets)), thread_name_prefix="hinata-read"
                ) as read_pool:
                    read_futs = {
                        read_pool.submit(self.fetch_webpage, item.url, max_chars=1800): item
                        for item in targets
                    }
                    for rfut in concurrent.futures.as_completed(read_futs):
                        src_item = read_futs[rfut]
                        try:
                            page = rfut.result()
                            if page.get("content"):
                                page_excerpts.append(
                                    {
                                        "title": str(page.get("title") or src_item.title)[:200],
                                        "url": src_item.url,
                                        "excerpt": str(page["content"])[:1800],
                                    }
                                )
                        except Exception:
                            continue

        return {
            "query": full_query,
            "core_query": core_query,
            "results": [r.to_dict() for r in deduped],
            "page_excerpts": page_excerpts,
            "count": len(deduped),
            "network_errors": provider_errors if not deduped else {},
            "connection_mode": "online" if deduped else "offline_fallback",
            "live_sources_available": bool(deduped),
        }
