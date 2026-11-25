"""Secret scanning and redaction before persistence, logs, prompts, or exports.

Defense in depth: prevents accidental persistence or exfiltration of passwords,
API keys, bearer tokens, private keys, and payment card numbers.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED_SECRET]"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,255}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,255}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,255}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Credentials embedded in URLs: postgres://user:pass@host, https://token@host, ...
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,255}\b")),
    # Google API keys are a fixed, unmistakable shape (AIza + 35 chars).
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    # Stripe/Anthropic-style sk_live_/sk_test_/sk-ant- keys.
    ("stripe_key", re.compile(r"\bsk_(?:live|test)_[0-9A-Za-z]{16,255}\b")),
    # SSH public-key material: the body is high-entropy base64.
    ("ssh_key", re.compile(r"\bssh-(?:rsa|ed25519|dss|ecdsa)[\w@.-]*\s+AAAA[A-Za-z0-9+/=]{20,}")),
    # Discord bot tokens: base64-ish id . timestamp . hmac
    ("discord_token", re.compile(r"\b[MNO][\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,40}\b")),
    # Twilio API keys are SK + 32 hex.
    ("twilio_key", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    # Any HTTP auth scheme: Bearer/Basic/Digest/Token/Negotiate. Short tokens
    # count too -- an 8-character API token is still a credential.
    (
        "auth_scheme_token",
        re.compile(
            r"(?i)\b(?:(?:Bearer|ApiKey)\s+[A-Za-z0-9._~+/=-]{8,1024}"      # unambiguous
            r"|(?:Basic|Digest|Token|Negotiate)\s+[A-Za-z0-9._~+/=-]{12,1024})\b"
        ),
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(password|passwd|pwd|api[_-]?key|apikey|access[_-]?token|secret[_-]?key|client[_-]?secret|pin|aws_secret_access_key|secret|master[_-]?password)\s*(?::|=|\bis\b)\s*([^\s,;\"']{4,256}|\"[^\"]{4,256}\"|'[^']{4,256}')"
        ),
    ),
    ("payment_card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    # A bare 64-char hex string is usually a checksum (this project prints SHA-256
    # digests everywhere), so only redact when a secret-ish word introduces it.
    # Raw header lines: the value after these names is a credential regardless
    # of shape (curl -v output, captured requests, proxy logs).
    (
        "auth_header",
        re.compile(
            r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|api-key)"
            r"\s*:\s*([^\r\n]{4,2048})"
        ),
    ),
    (
        "hex_secret",
        re.compile(r"(?i)\b\w*(?:key|secret|token|salt|hmac|passphrase)\w*\s*(?::|=|\bis\b)\s*([0-9a-f]{32,128})\b"),
    ),
)

# Key names (already normalized) whose *values* are secret even when the value
# itself matches no token pattern -- e.g. a bare password or a raw hex key.
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:password|passwd|passphrase|pwd|secret|secret[_-]?key|api[_-]?key|apikey"
    r"|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|client[_-]?secret"
    r"|private[_-]?key|signing[_-]?key|encryption[_-]?key|master[_-]?key|pin"
    # Credential-bearing header / field names. These carry an opaque value that
    # no token-shape rule can recognise, so the key itself must force redaction.
    r"|authorization|proxy[_-]?authorization|x[_-]?api[_-]?key|api[_-]?key"
    r"|cookie|set[_-]?cookie|session[_-]?id|session[_-]?key|auth|credentials"
    r"|bearer|jwt|id[_-]?token|signature|signing[_-]?secret|webhook[_-]?secret)"
)


def _luhn_valid(digits: str) -> bool:
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    reverse = digits[::-1]
    for idx, ch in enumerate(reverse):
        n = ord(ch) - 48
        if idx % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def detect_secrets(text: str) -> list[str]:
    """Return distinct secret category names detected in ``text``."""
    if not text:
        return []
    found: list[str] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            if kind == "payment_card":
                digits = re.sub(r"\D", "", match.group(0))
                if not _luhn_valid(digits):
                    continue
            if kind not in found:
                found.append(kind)
            break
    return found


def contains_secrets(text: str) -> bool:
    return bool(detect_secrets(text))


def redact_text(text: str) -> str:
    """Redact recognized secrets from ``text`` while keeping surrounding text readable."""
    if not text:
        return text
    result = text
    for kind, pattern in _PATTERNS:
        if kind == "credential_assignment":
            result = pattern.sub(lambda m: f"{m.group(1)}=REDACTED_PLACEHOLDER", result)
            result = result.replace("REDACTED_PLACEHOLDER", REDACTED)
        elif kind == "auth_header":
            # Keep the header name so the log still says what was sent.
            result = pattern.sub(lambda m: f"{m.group(1)}: {REDACTED}", result)
        elif kind == "url_credentials":
            # Keep the scheme and host; drop the user:password pair entirely.
            result = pattern.sub(lambda m: f"{m.group(1)}[REDACTED_CREDENTIALS]@", result)
        elif kind == "hex_secret":
            result = pattern.sub(lambda m: m.group(0).replace(m.group(1), REDACTED), result)
        elif kind == "payment_card":
            def _replace_card(match: re.Match[str]) -> str:
                raw = match.group(0)
                digits = re.sub(r"\D", "", raw)
                return f"[REDACTED_CARD:{digits[-4:]}]" if _luhn_valid(digits) else raw
            result = pattern.sub(_replace_card, result)
        else:
            result = pattern.sub(f"[{REDACTED[1:-1]}:{kind}]", result)
    return result


def redact_value(value: Any, *, max_depth: int = 8) -> Any:
    """Recursively redact secrets in strings, lists, tuples, and dicts.

    A value is redacted when its own text matches a known token pattern *or* when
    its key names a secret field (``password``, ``token``, ``client_secret``, ...).
    Key-based redaction is required because a bare value such as ``hunter2`` or a
    raw hex key has no recognizable shape of its own.
    """
    if max_depth <= 0:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if _SENSITIVE_KEY.search(key_str.strip()):
                sanitized[key_str] = REDACTED
            else:
                sanitized[key_str] = redact_value(v, max_depth=max_depth - 1)
        return sanitized
    if isinstance(value, list):
        return [redact_value(item, max_depth=max_depth - 1) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, max_depth=max_depth - 1) for item in value)
    return value
