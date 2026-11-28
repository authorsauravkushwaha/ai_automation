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

import http.client
import ipaddress
import socket
import time
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


def is_explicit_loopback_url(url: str) -> bool:
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_loopback_opener() -> urllib.request.OpenerDirector:
    """Opener for trusted loopback services: no proxies, no redirects.

    Proxies are disabled because an ``HTTP_PROXY`` inherited from the shell
    would otherwise route loopback traffic (and the prompt inside it) through a
    third-party proxy.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirectHandler())


def public_connection(address: tuple[str, int], timeout: Any = None,
                      source_address: Any = None, **kwargs: Any) -> socket.socket:
    """Resolve once at connect time, validate the entire answer, connect numeric IPs.

    TLS still uses the original hostname in HTTPSConnection (SNI + verification).
    No ambient proxy, second DNS lookup, private answer, or address-family fallback
    can change the destination after validation. DNS itself is OS-resolver timed.
    """
    host, port = address
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers or len(answers) > 64:
        raise OSError("Public destination has no bounded DNS answer")
    for family, _, _, _, sockaddr in answers:
        ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        if (family not in {socket.AF_INET, socket.AF_INET6} or not ip.is_global
                or ip.is_multicast or ip.is_reserved or getattr(ip, "ipv4_mapped", None)
                or "%" in str(sockaddr[0])):
            raise OSError("Refusing non-public connect-time address")
    seconds = 30.0 if timeout is None or not isinstance(timeout, (int, float)) else float(timeout)
    deadline = time.monotonic() + max(0.001, seconds)
    error: OSError = OSError("Public connection failed")
    for family, socktype, proto, _, sockaddr in answers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Public connection deadline exceeded")
        connection = socket.socket(family, socktype, proto)
        try:
            connection.settimeout(remaining)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)  # numeric sockaddr from the validated answer
            return connection
        except OSError as exc:
            error = exc
            connection.close()
    raise error


class PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class PublicHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, check_url: Callable[[str], Any]) -> None:
        super().__init__()
        self.check_url = check_url

    def http_open(self, req: Any) -> Any:
        self.check_url(req.full_url)
        return self.do_open(PublicHTTPConnection, req)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, check_url: Callable[[str], Any]) -> None:
        super().__init__()
        self.check_url = check_url

    def https_open(self, req: Any) -> Any:
        self.check_url(req.full_url)
        return self.do_open(PublicHTTPSConnection, req, context=getattr(self, "_context", None))


def build_public_web_opener(check_url: Callable[[str], Any], *, follow_redirects: bool = True) -> urllib.request.OpenerDirector:
    """Opener for public web access whose every redirect hop is re-validated."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       PublicHTTPHandler(check_url), PublicHTTPSHandler(check_url),
                                       ValidatedRedirectHandler(check_url) if follow_redirects else NoRedirectHandler())
