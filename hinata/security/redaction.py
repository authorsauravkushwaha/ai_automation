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
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{16,255}\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,512}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(password|passwd|pwd|api[_-]?key|access[_-]?token|secret[_-]?key|client[_-]?secret|pin)\s*(?::|=|\bis\b)\s*([^\s,;\"']{4,256}|\"[^\"]{4,256}\"|'[^']{4,256}')"
        ),
    ),
    ("payment_card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
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
            result = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", result)
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
    """Recursively redact secrets in strings, lists, tuples, and dicts."""
    if max_depth <= 0:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if re.search(r"(?i)^(password|passwd|secret|api_?key|access_?token|refresh_?token|pin|private_?key)$", key_str):
                sanitized[key_str] = REDACTED
            else:
                sanitized[key_str] = redact_value(v, max_depth=max_depth - 1)
        return sanitized
    if isinstance(value, list):
        return [redact_value(item, max_depth=max_depth - 1) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, max_depth=max_depth - 1) for item in value)
    return value
