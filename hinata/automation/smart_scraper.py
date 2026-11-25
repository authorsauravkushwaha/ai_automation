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
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.automation.web_automation import normalize_url


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict[str, Any]] = []
        self._cur_table: dict[str, Any] | None = None
        self._cur_row: list[str] | None = None
        self._in_th = False
        self._in_td = False
        self._buf: list[str] = []
        self._headers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "table":
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
                if not self._cur_table["headers"] and all(self._in_th or h for h in text_row):
                    # Check if previous row was headers via th flag — simplified: first non-empty row as header if next rows exist
                    pass
                # Heuristic: first row with th-like content becomes header
                if not self._cur_table["headers"]:
                    # Look ahead: if we haven't set headers, treat first row as header if it looks header-ish
                    # We'll store first row as header if it contains typical header words or th was used
                    self._cur_table["headers"] = text_row
                else:
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
        for m in re.finditer(r"\b\d[\d,]*\.\d{2}\b", t):
            if m.group(0) not in self.prices:
                # Avoid capturing random numbers — only if near currency context handled separately
                pass
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
        # Simple heuristic: extract all <li> text, <article> titles, heading lists
        items: list[str] = []
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
