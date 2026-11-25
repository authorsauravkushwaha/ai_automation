"""Central network-egress guards shared by every urllib-based HTTP client.

HINATA talks to the network from exactly two kinds of call sites:

* the **loopback** Ollama runtime and the optional local image backend, and
* the **opt-in public web** fetchers used by web search and scraping.

Both are security-sensitive and both must refuse unsafe redirects. Keeping the
redirect policy in one audited place -- instead of four near-identical classes
spread across ``core`` and ``automation`` -- means a fix or a review covers
every client at once.

Threat model
------------
A redirect is a second, attacker-influenced network request. Two failure modes
matter:

1. A loopback service (or anything able to answer on 127.0.0.1) returns
   ``302 Location: https://attacker.example`` and the client follows it,
   leaking the user's prompt, or worse, letting the redirect chain bounce the
   client back into a private network range (SSRF).
2. A public page redirects into ``169.254.169.254`` (cloud metadata),
   ``10.0.0.0/8`` or ``127.0.0.1`` after the first hop was validated.

``NoRedirectHandler`` blocks (1) absolutely. ``ValidatedRedirectHandler`` blocks
(2) by re-running the caller's own public-URL policy on every hop.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every HTTP redirect, returning the 3xx response to the caller.

    Used for loopback clients (Ollama, local image backend): a redirect from a
    local service is never legitimate, and following it could forward a private
    prompt to an arbitrary host.
    """

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only when the resolved target passes ``check_url``.

    ``check_url`` is the caller's policy callback (for example
    :func:`hinata.automation.web_search.is_safe_public_url`). It must raise to
    reject a target; the exception propagates and aborts the request.
    """

    def __init__(self, check_url: Callable[[str], Any]) -> None:
        super().__init__()
        self._check_url = check_url

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        resolved = urljoin(req.full_url, newurl)
        self._check_url(resolved)
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def build_loopback_opener() -> urllib.request.OpenerDirector:
    """Opener for trusted loopback services: no proxies, no redirects.

    Proxies are disabled because an ``HTTP_PROXY`` inherited from the shell
    would otherwise route loopback traffic (and the prompt inside it) through a
    third-party proxy.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirectHandler())


def build_public_web_opener(check_url: Callable[[str], Any]) -> urllib.request.OpenerDirector:
    """Opener for public web access whose every redirect hop is re-validated."""
    return urllib.request.build_opener(ValidatedRedirectHandler(check_url))
