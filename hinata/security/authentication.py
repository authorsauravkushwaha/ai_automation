"""PIN verification for sensitive actions. Voice is never treated as a PIN."""

from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any

from hinata.database.database import Database


class Authentication:
    SETTING_KEY = "owner_pin_pbkdf2"
    LOCKOUT_KEY = "owner_pin_attempts"
    ITERATIONS = 210_000
    MIN_ITERATIONS = 100_000
    MAX_ITERATIONS = 1_000_000
    SALT_BYTES = 16
    MAX_FAILED_ATTEMPTS = 5
    LOCKOUT_SECONDS = 60.0

    def __init__(self, database: Database) -> None:
        self.database = database
        self._lock = threading.Lock()

    @property
    def is_configured(self) -> bool:
        return self.database.get_setting(self.SETTING_KEY) is not None

    def set_pin(self, pin: str) -> None:
        if (
            not isinstance(pin, str)
            or not pin.isascii()
            or not pin.isdigit()
            or not 4 <= len(pin) <= 12
        ):
            raise ValueError("PIN must contain 4 to 12 ASCII digits")
        salt = secrets.token_bytes(self.SALT_BYTES)
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, self.ITERATIONS)
        self.database.set_setting(
            self.SETTING_KEY, f"pbkdf2_sha256${self.ITERATIONS}${salt.hex()}${digest.hex()}"
        )
        self._clear_lockout()

    def _read_lockout(self) -> dict[str, Any]:
        raw = self.database.get_setting(self.LOCKOUT_KEY)
        if not raw:
            return {"failures": 0, "locked_until": 0.0}
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return {"failures": 0, "locked_until": 0.0}
        if not isinstance(decoded, dict):
            return {"failures": 0, "locked_until": 0.0}
        try:
            failures = max(0, min(int(decoded.get("failures", 0)), self.MAX_FAILED_ATTEMPTS))
            locked_until = max(0.0, float(decoded.get("locked_until", 0.0)))
        except (TypeError, ValueError, OverflowError):
            return {"failures": 0, "locked_until": 0.0}
        return {"failures": failures, "locked_until": locked_until}

    def _write_lockout(self, record: dict[str, Any]) -> None:
        self.database.set_setting(self.LOCKOUT_KEY, json.dumps(record, separators=(",", ":")))

    def _clear_lockout(self) -> None:
        self.database.execute("DELETE FROM settings WHERE key = ?", (self.LOCKOUT_KEY,))

    def lockout_remaining(self) -> float:
        """Seconds remaining in a PIN lockout; never more than one lockout window."""
        with self._lock:
            record = self._read_lockout()
            now = time.time()
            # A corrupt or hostile database value cannot lock the owner out
            # forever. Do not discard ordinary failure counters: doing so here
            # used to reset the counter on *every* failed attempt, so the
            # configured fifth failure never triggered a lockout.
            if record["locked_until"] > now:
                return min(record["locked_until"], now + self.LOCKOUT_SECONDS) - now
            if record["locked_until"]:
                self._clear_lockout()
            return 0.0

    def _record_failure(self) -> None:
        with self._lock:
            record = self._read_lockout()
            now = time.time()
            failures = record["failures"] + 1
            locked_until = 0.0
            if failures >= self.MAX_FAILED_ATTEMPTS:
                locked_until = now + self.LOCKOUT_SECONDS
                failures = 0
            self._write_lockout({"failures": failures, "locked_until": locked_until})

    @staticmethod
    def _parse_stored(stored: str) -> tuple[str, int, bytes, bytes] | None:
        try:
            algorithm, iterations_text, salt_hex, expected_hex = stored.split("$", 3)
            iterations = int(iterations_text)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(expected_hex)
        except (ValueError, TypeError, binascii.Error):
            return None
        if (
            algorithm != "pbkdf2_sha256"
            or not Authentication.MIN_ITERATIONS <= iterations <= Authentication.MAX_ITERATIONS
            or len(salt) != Authentication.SALT_BYTES
            or len(expected) != 32
        ):
            return None
        return algorithm, iterations, salt, expected

    def verify_pin(self, pin: object) -> bool:
        stored = self.database.get_setting(self.SETTING_KEY)
        if not stored or not isinstance(pin, str) or not pin:
            return False
        if self.lockout_remaining() > 0:
            return False
        # Reject malformed input before PBKDF2. A non-ASCII PIN used to raise
        # UnicodeEncodeError here instead of simply being refused.
        if not (pin.isascii() and pin.isdigit() and 4 <= len(pin) <= 12):
            return False
        parsed = self._parse_stored(stored)
        if parsed is None:
            return False
        _, iterations, salt, expected = parsed
        try:
            actual = hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, iterations)
        except (ValueError, TypeError, OverflowError):
            return False
        if hmac.compare_digest(actual, expected):
            self._clear_lockout()
            return True
        self._record_failure()
        return False

    def clear_pin(self) -> None:
        self.database.execute("DELETE FROM settings WHERE key = ?", (self.SETTING_KEY,))
        self._clear_lockout()
