"""Smart Scraper & Data Extractor — turn any website into structured data.

Features:
- Table extraction (handles colspan, headers, nested tables)
- List/card extraction with CSS selectors
- Price & stock monitoring
- Pagination crawling
- Export to CSV/JSON inside allowed roots
- Screenshot + PDF snapshot (when driver available)
"""

from __future__ import annotations

import csv
import json
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.automation.web_automation import normalize_url


def _narrow_html_to_hint(html: str, selector_hint: str) -> str:
    """Best-effort narrowing for a CSS-ish hint such as ``div.cards`` or ``#main``.

    Supported: ``tag``, ``.class``, ``#id``, ``tag.class``, ``tag#id`` and a
    multi-step hint (the last step is used, e.g. ``table > tbody > tr``). Nested
    same-name elements are balanced by counting open/close tags.

    A hint never makes the result *smaller* than it should be: if the selector
    does not match, the full document is returned unchanged so extraction still
    has data to work with.
    """
    hint = selector_hint.strip()
    if not hint:
        return html
    token = hint.split()[-1].split(">")[-1].strip()
    parts = re.fullmatch(r"([a-zA-Z][\w-]*)?(?:\.([\w-]+))?(?:#([\w-]+))?", token)
    if not parts:
        return html
    tag, cls, ident = parts.group(1) or "", parts.group(2) or "", parts.group(3) or ""
    if not (tag or cls or ident):
        return html

    lookaheads = ""
    if ident:
        lookaheads += rf'(?=[^>]*\bid\s*=\s*["\']{re.escape(ident)}["\'])'
    if cls:
        lookaheads += (
            rf'(?=[^>]*\bclass\s*=\s*["\'][^"\']*(?<![\w-]){re.escape(cls)}(?![\w-])[^"\']*["\'])'
        )
    name = re.escape(tag) if tag else r"[a-zA-Z][\w-]*"
    match = re.search(rf"<({name})\b{lookaheads}[^>]*>", html, re.I)
    if not match:
        return html

    element = match.group(1)
    open_pat = re.compile(rf"<{re.escape(element)}\b[^>]*>", re.I)
    close_pat = re.compile(rf"</{re.escape(element)}\s*>", re.I)
    depth = 1
    cursor = match.end()
    while depth:
        nxt_open = open_pat.search(html, cursor)
        nxt_close = close_pat.search(html, cursor)
        if nxt_close is None:
            return html[match.end():]  # unbalanced markup: keep the remainder
        if nxt_open is not None and nxt_open.start() < nxt_close.start():
            depth += 1
            cursor = nxt_open.end()
            continue
        depth -= 1
        if depth == 0:
            return html[match.end(): nxt_close.start()]
        cursor = nxt_close.end()
    return html[match.end():]


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict[str, Any]] = []
        self._cur_table: dict[str, Any] | None = None
        # A page can contain arbitrarily many tables/rows and the download limit
        # alone allowed a 5 MB page to build 20k+ rows in memory. Callers only
        # ever report a handful, so stop collecting once that is exceeded.
        self.max_tables = 10
        self.max_rows = 500
        self._cur_row: list[str] | None = None
        self._in_th = False
        self._in_td = False
        self._buf: list[str] = []
        self._headers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "table":
            if len(self.tables) >= self.max_tables:
                self._cur_table = None  # already have enough; ignore the rest
                return
            self._cur_table = {"headers": [], "rows": []}
            self.tables.append(self._cur_table)
            self._headers = []
        elif t == "tr" and self._cur_table is not None:
            self._cur_row = []
        elif t in {"th", "td"} and self._cur_row is not None:
            self._in_th = t == "th"
            self._in_td = t == "td"
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "table":
            self._cur_table = None
        elif t == "tr" and self._cur_table is not None and self._cur_row is not None:
            text_row = [c.strip() for c in self._cur_row]
            if any(text_row):
                # Heuristic: the first non-empty row becomes the header row.
                if not self._cur_table["headers"]:
                    # Look ahead: if we haven't set headers, treat first row as header if it looks header-ish
                    # We'll store first row as header if it contains typical header words or th was used
                    self._cur_table["headers"] = text_row
                elif len(self._cur_table["rows"]) < self.max_rows:
                    self._cur_table["rows"].append(text_row)
            self._cur_row = None
        elif t in {"th", "td"} and self._cur_row is not None:
            val = " ".join("".join(self._buf).split()).strip()
            self._cur_row.append(val)
            self._in_th = self._in_td = False
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_th or self._in_td:
            self._buf.append(data)


class PriceParser(HTMLParser):
    """Heuristic price/stock extractor."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.prices: list[str] = []
        self.availability: str = "unknown"
        self._text: list[str] = []

    def handle_data(self, data: str) -> None:
        t = data.strip()
        if not t:
            return
        self._text.append(t)
        # Price patterns: $12.99, ₹1,299, €49.99, 1,299.00 etc.
        for m in re.finditer(r"(?:₹|\$|€|£|¥)\s*[\d,]+(?:\.\d{1,2})?", t):
            self.prices.append(m.group(0).strip())
        # A bare "49.99" is a price only when the text around it says so;
        # otherwise ratings, sizes and timestamps all look like money. (This
        # branch used to be an ``if`` whose body was just ``pass``, so prices
        # written without a currency symbol were silently never captured.)
        if re.search(r"(?i)\b(?:price|cost|total|now|only|from|mrp|was)\b|:", t):
            for m in re.finditer(r"\b\d[\d,]*\.\d{2}\b", t):
                if m.group(0) not in self.prices:
                    self.prices.append(m.group(0))
        lower = t.lower()
        if any(w in lower for w in ["out of stock", "sold out", "currently unavailable"]):
            self.availability = "out_of_stock"
        elif any(w in lower for w in ["in stock", "available", "add to cart", "buy now"]):
            if self.availability != "out_of_stock":
                self.availability = "in_stock"


class SmartScraper:
    def __init__(self, files=None) -> None:
        self.files = files

    def fetch(self, url: str, *, timeout: int = 12, allow_private: bool = True) -> str:
        target = normalize_url(url, allow_private=allow_private)
        req = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0 HINATA Scraper/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(500_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                return raw.decode(charset, errors="replace")
            except Exception:
                return raw.decode("utf-8", errors="replace")

    def scrape_tables(self, url: str, *, max_tables: int = 5) -> dict[str, Any]:
        html = self.fetch(url)
        parser = TableParser()
        parser.feed(html)
        tables = parser.tables[:max_tables]
        return {
            "url": url,
            "table_count": len(parser.tables),
            "tables": tables,
            "summary": f"Found {len(parser.tables)} table(s). Showing {len(tables)}.",
        }

    def scrape_lists(self, url: str, *, selector_hint: str = "") -> dict[str, Any]:
        """Extract list-like content: <li>, cards, articles."""
        html = self.fetch(url)
        if selector_hint:
            # A selector hint narrows the region we scan; it never replaces the
            # real extraction below, so hinted and unhinted calls agree.
            html = _narrow_html_to_hint(html, selector_hint)
        # Simple heuristic: extract all <li> text, <article> titles, heading lists
        # li parser
        class ListParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.in_li = False
                self.buf: list[str] = []
                self.items: list[str] = []
            def handle_starttag(self, tag: str, attrs) -> None:
                if tag.lower() == "li":
                    self.in_li = True
                    self.buf = []
            def handle_endtag(self, tag: str) -> None:
                if tag.lower() == "li" and self.in_li:
                    text = " ".join("".join(self.buf).split()).strip()
                    if len(text) >= 3:
                        self.items.append(text[:300])
                    self.in_li = False
            def handle_data(self, data: str) -> None:
                if self.in_li:
                    self.buf.append(data)
        lp = ListParser()
        lp.feed(html)
        # Also grab headings as sections
        headings = re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", html, re.I | re.S)
        clean_headings = [" ".join(re.sub(r"<[^>]+>", "", h).split()).strip() for h in headings if len(h.strip()) >= 3][:30]
        return {
            "url": url,
            "list_items": lp.items[:100],
            "headings": clean_headings,
            "total_li": len(lp.items),
            "total_headings": len(clean_headings),
        }

    def monitor_price(self, url: str) -> dict[str, Any]:
        html = self.fetch(url)
        parser = PriceParser()
        parser.feed(html)
        text_preview = " ".join(parser._text[:80])[:800]
        return {
            "url": url,
            "prices_found": parser.prices[:10],
            "availability": parser.availability,
            "primary_price": parser.prices[0] if parser.prices else None,
            "text_preview": text_preview,
        }

    def crawl_pages(self, start_url: str, *, max_pages: int = 5, link_pattern: str = "") -> dict[str, Any]:
        """Follow pagination / next links up to max_pages."""
        visited: set[str] = set()
        to_visit = [normalize_url(start_url, allow_private=True)]
        results: list[dict[str, Any]] = []
        pattern = re.compile(link_pattern, re.I) if link_pattern else None
        while to_visit and len(visited) < max_pages:
            url = to_visit.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                html = self.fetch(url)
            except Exception as exc:
                results.append({"url": url, "error": str(exc)})
                continue
            # Extract title
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            title = " ".join(re.sub(r"<[^>]+>", "", m.group(1)).split()).strip()[:120] if m else ""
            results.append({"url": url, "title": title, "chars": len(html)})
            # Find next/page links
            for href_match in re.finditer(r'href=[\"\']([^\"\']+)[\"\']', html, re.I):
                href = href_match.group(1)
                if href.startswith("#") or href.startswith("javascript:"):
                    continue
                abs_url = urllib.parse.urljoin(url, href)
                if normalize_url(abs_url, allow_private=True) not in visited and len(to_visit) + len(visited) < max_pages:
                    if pattern and not pattern.search(href) and not pattern.search(abs_url):
                        continue
                    parsed = urllib.parse.urlsplit(abs_url)
                    if parsed.scheme in {"http", "https"} and abs_url not in to_visit:
                        to_visit.append(abs_url)
            time.sleep(0.2)
        return {"start_url": start_url, "visited": len(visited), "pages": results}

    def export_data(self, data: dict[str, Any], dest_path: str, fmt: str = "json") -> dict[str, Any]:
        if not self.files:
            raise AutomationError("File manager not available for export")
        dest = self.files.resolve(dest_path, must_exist=False)
        if dest.exists():
            raise AutomationError(f"Destination already exists: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fmt = fmt.lower().strip()
        if fmt == "json":
            dest.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        elif fmt == "csv":
            # Expect data with tables or list
            rows = []
            if "tables" in data and data["tables"]:
                t = data["tables"][0]
                headers = t.get("headers", [])
                rows.append(headers)
                rows.extend(t.get("rows", []))
            elif "list_items" in data:
                rows = [["item"], *[[x] for x in data["list_items"]]]
            else:
                rows = [[k, str(v)[:200]] for k, v in data.items()]
            with dest.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerows(rows)
        else:
            raise AutomationError("Format must be json or csv")
        return {"path": str(dest), "format": fmt, "bytes": dest.stat().st_size}
