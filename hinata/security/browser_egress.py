"""Fail-closed page egress for owner-approved Playwright sessions.

All HTTP is fulfilled by the pinned urllib client, never route.continue_ or
route.fetch (both would delegate DNS to the browser). Same-origin only; service
workers, WebSockets and downloads are disabled. This is page-level containment,
not a claim to replace an OS firewall for native Chromium processes.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from hinata.security.net_guards import (
    build_loopback_opener,
    build_public_web_opener,
    is_explicit_loopback_url,
)


def origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only credential-free HTTP(S) page requests are permitted")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


class BrowserEgress:
    def __init__(self, url: str) -> None:
        self.allowed_origin = origin(url)
        self.requests = 0
        self.bytes_read = 0
        self.blocked = 0
        self.deadline = time.monotonic() + 60
        self.opener = (build_loopback_opener() if is_explicit_loopback_url(url) else
                       build_public_web_opener(self.validate, follow_redirects=False))

    def validate(self, url: str) -> None:
        if origin(url) != self.allowed_origin:
            raise ValueError("Cross-origin page egress blocked; use a separately approved session")

    def handle(self, route: Any) -> None:
        try:
            request = route.request
            self.requests += 1
            self.validate(request.url)
            remaining = self.deadline - time.monotonic()
            if self.requests > 100 or self.bytes_read >= 16_000_000 or remaining <= 0:
                raise ValueError("Browser network budget exhausted")
            data = request.post_data_buffer
            if data and len(data) > 1_000_000:
                raise ValueError("Browser request body exceeds limit")
            headers = {k: v for k, v in request.all_headers().items()
                       if k.lower() not in {"host", "connection", "proxy-authorization", "proxy-connection",
                                            "content-length", "transfer-encoding", "accept-encoding"}}
            headers["Accept-Encoding"] = "identity"
            req = urllib.request.Request(request.url, data=data, headers=headers, method=request.method)
            try:
                response = self.opener.open(req, timeout=min(10, remaining))
            except urllib.error.HTTPError as exc:
                response = exc  # Forward status, including redirects; never follow invisibly.
            with response:
                # Browser redirect hops are not guaranteed to re-enter route callbacks.
                # Validate Location BEFORE exposing the redirect to Chromium.
                if 300 <= response.code < 400 and response.headers.get("Location"):
                    self.validate(urllib.parse.urljoin(request.url, response.headers["Location"]))
                allowance = min(4_000_000, 16_000_000 - self.bytes_read)
                chunks = bytearray()
                read = getattr(response, "read1", response.read)
                response_deadline = min(self.deadline, time.monotonic() + 10)
                while True:
                    if time.monotonic() >= response_deadline:
                        raise TimeoutError("Browser response deadline exceeded")
                    chunk = read(min(65536, allowance + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    self.bytes_read += len(chunk)
                    if len(chunks) > allowance:
                        raise ValueError("Browser response exceeds limit")
                body = bytes(chunks)
                outgoing = {k: v for k, v in response.headers.items()
                            if k.lower() not in {"connection", "transfer-encoding", "content-length"}}
                route.fulfill(status=response.code, headers=outgoing, body=body)
        except Exception:
            self.blocked += 1
            route.abort("blockedbyclient")

    def block_websocket(self, websocket: Any) -> None:
        self.blocked += 1
        websocket.close()


@contextmanager
def guarded_page(playwright: Any, url: str, *, headless: bool = False,
                 executable_path: str | None = None) -> Iterator[tuple[Any, BrowserEgress]]:
    policy = BrowserEgress(url)
    browser = playwright.chromium.launch(
        headless=headless,
        executable_path=executable_path,
        proxy={"server": "http://127.0.0.1:9"},  # No fallback to unmediated browser HTTP.
        args=["--disable-background-networking", "--disable-quic", "--proxy-bypass-list=<-loopback>",
              "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
    )
    try:
        context = browser.new_context(service_workers="block", accept_downloads=False)
        if not callable(getattr(context, "route_web_socket", None)):
            raise RuntimeError("Guarded automation needs Playwright with WebSocket routing support")
        context.route("**/*", policy.handle)
        context.route_web_socket("**/*", policy.block_websocket)
        yield context.new_page(), policy
    finally:
        browser.close()
