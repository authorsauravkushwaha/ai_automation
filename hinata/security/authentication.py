"""PIN verification for sensitive actions. Voice is never treated as a PIN."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from hinata.database.database import Database


class Authentication:
    SETTING_KEY = "owner_pin_pbkdf2"
    ITERATIONS = 210_000

    def __init__(self, database: Database) -> None:
        self.database = database

    @property
    def is_configured(self) -> bool:
        return self.database.get_setting(self.SETTING_KEY) is not None

    def set_pin(self, pin: str) -> None:
        if not pin.isascii() or not pin.isdigit() or not 4 <= len(pin) <= 12:
            raise ValueError("PIN must contain 4 to 12 ASCII digits")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, self.ITERATIONS)
        self.database.set_setting(
            self.SETTING_KEY, f"pbkdf2_sha256${self.ITERATIONS}${salt.hex()}${digest.hex()}"
        )

    def verify_pin(self, pin: str) -> bool:
        stored = self.database.get_setting(self.SETTING_KEY)
        if not stored or not pin:
            return False
        try:
            algorithm, iterations, salt_hex, expected_hex = stored.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            actual = hashlib.pbkdf2_hmac(
                "sha256", pin.encode("ascii"), bytes.fromhex(salt_hex), int(iterations)
            )
            return hmac.compare_digest(actual.hex(), expected_hex)
        except (ValueError, TypeError):
            return False

    def clear_pin(self) -> None:
        self.database.execute("DELETE FROM settings WHERE key = ?", (self.SETTING_KEY,))
