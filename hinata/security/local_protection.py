"""Protect biometric and sensitive local data with Windows DPAPI or authenticated local keys.

Provides:
- Windows DPAPI protection for owner voice profiles (backward compatible).
- Versioned AES-256-GCM envelopes using the optional cryptography package.
- Read-only compatibility with legacy v1 envelopes; new writes never use v1.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from hinata.config import restrict_private_path


class ProtectionError(RuntimeError):
    pass


ENVELOPE_PREFIX = "enc:v2:"
LEGACY_ENVELOPE_PREFIX = "enc:v1:"
AEAD_ITERATIONS = 600_000
KEY_ITERATIONS = 210_000


def _derive_keys(secret: bytes, salt: bytes) -> tuple[bytes, bytes]:
    material = hashlib.pbkdf2_hmac("sha256", secret, salt, KEY_ITERATIONS, dklen=64)
    return material[:32], material[32:]


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    blocks: list[bytes] = []
    counter = 0
    needed = (length + 31) // 32
    while counter < needed:
        block_input = nonce + counter.to_bytes(8, "big")
        blocks.append(hmac.new(enc_key, block_input, hashlib.sha256).digest())
        counter += 1
    return b"".join(blocks)[:length]


def _aesgcm() -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise ProtectionError("AES-GCM storage requires: pip install 'hinata-local-assistant[secure-storage]'") from exc
    return AESGCM


def encrypt_envelope(plaintext: bytes, secret: bytes, *, aad: bytes = b"") -> str:
    """Write only v2 AES-256-GCM; no silent fallback to custom cryptography."""
    if not secret:
        raise ProtectionError("Encryption key material must not be empty")
    aes = _aesgcm()
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    key = hashlib.pbkdf2_hmac("sha256", secret, salt, AEAD_ITERATIONS, dklen=32)
    ciphertext = aes(key).encrypt(nonce, plaintext, b"HINATA-ENC-V2|" + aad)
    return ENVELOPE_PREFIX + base64.urlsafe_b64encode(salt + nonce + ciphertext).decode("ascii")


def decrypt_envelope(token: str, secret: bytes, *, aad: bytes = b"") -> bytes:
    """Verify MAC and decrypt an envelope produced by ``encrypt_envelope``."""
    if token.startswith(ENVELOPE_PREFIX):
        if not secret:
            raise ProtectionError("Decryption key material must not be empty")
        aes = _aesgcm()
        try:
            packed = base64.b64decode(token[len(ENVELOPE_PREFIX):].encode("ascii"), altchars=b"-_", validate=True)
            if len(packed) < 44:
                raise ValueError("Truncated envelope")
            salt, nonce, ciphertext = packed[:16], packed[16:28], packed[28:]
            key = hashlib.pbkdf2_hmac("sha256", secret, salt, AEAD_ITERATIONS, dklen=32)
            return aes(key).decrypt(nonce, ciphertext, b"HINATA-ENC-V2|" + aad)
        except Exception as exc:
            raise ProtectionError("Encrypted data authentication failed (wrong key, context or tampered payload)") from exc
    if not token.startswith(LEGACY_ENVELOPE_PREFIX):
        raise ProtectionError("Invalid encrypted envelope header")
    if not secret:
        raise ProtectionError("Decryption key material must not be empty")
    try:
        packed = base64.urlsafe_b64decode(token[len(LEGACY_ENVELOPE_PREFIX) :].encode("ascii"))
    except Exception as exc:
        raise ProtectionError("Malformed base64 encrypted envelope") from exc
    if len(packed) < 64:
        raise ProtectionError("Truncated encrypted envelope")
    salt = packed[:16]
    nonce = packed[16:32]
    expected_tag = packed[32:64]
    ciphertext = packed[64:]
    enc_key, mac_key = _derive_keys(secret, salt)
    mac_input = b"HINATA-ENC-V1|" + salt + nonce + len(aad).to_bytes(4, "big") + aad + ciphertext
    actual_tag = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_tag, actual_tag):
        raise ProtectionError("Encrypted data authentication failed (wrong key or tampered payload)")
    stream = _keystream(enc_key, nonce, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream, strict=True))


class LocalVault:
    """Manages an owner-only local master key (DPAPI-wrapped on Windows, 0600 keyfile elsewhere)."""

    def __init__(self, data_dir: Path, *, enabled: bool = False) -> None:
        self.data_dir = data_dir
        self.enabled = enabled
        self.key_path = data_dir / "vault.key"
        self._cached_key: bytes | None = None

    def ensure_key(self) -> bytes:
        if self._cached_key is not None:
            return self._cached_key
        if self.key_path.is_file():
            raw = read_private(self.key_path)
            if len(raw) < 32:
                raise ProtectionError("Local vault key is corrupted")
            self._cached_key = raw
            return raw
        key = secrets.token_bytes(32)
        if not _create_private_key_if_absent(self.key_path, key):
            # Another process won the creation race; use its key, never replace it.
            raw = read_private(self.key_path)
            if len(raw) < 32:
                raise ProtectionError("Local vault key is corrupted")
            self._cached_key = raw
            return raw
        self._cached_key = key
        return key

    def rotate_key(self) -> bytes:
        key = secrets.token_bytes(32)
        write_private(self.key_path, key)
        self._cached_key = key
        return key

    def destroy_key(self) -> bool:
        self._cached_key = None
        if self.key_path.exists():
            # Overwrite once before unlink as best-effort local hygiene.
            try:
                self.key_path.write_bytes(b"\x00" * 64)
            except OSError:
                pass
            self.key_path.unlink()
            return True
        return False

    def protect_text(self, value: str, *, context: str = "") -> str:
        if not self.enabled or not value:
            return value
        if value.startswith("enc:"):
            plaintext = self.unprotect_text(value, context=context)  # authenticate before treating as sealed
            if value.startswith(ENVELOPE_PREFIX):
                return value
            value = plaintext  # explicitly touched legacy fields upgrade on write
        key = self.ensure_key()
        return encrypt_envelope(value.encode("utf-8"), key, aad=context.encode("utf-8"))

    def unprotect_text(self, value: str, *, context: str = "") -> str:
        if not value or not value.startswith("enc:"):
            return value
        if self._cached_key is None and not self.key_path.is_file():
            raise ProtectionError("Existing vault key is missing; restore it before decrypting")
        key = self.ensure_key()
        return decrypt_envelope(value, key, aad=context.encode("utf-8")).decode("utf-8")

    def export_encrypted_json(self, payload: dict[str, Any], passphrase: str, destination: Path) -> Path:
        if len(passphrase) < 8:
            raise ProtectionError("Backup passphrase must be at least 8 characters")
        serialized = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        envelope = encrypt_envelope(serialized, passphrase.encode("utf-8"), aad=b"hinata-backup-v1")
        wrapper = {
            "format": "hinata-encrypted-backup",
            "version": 1,
            "envelope": envelope,
        }
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".hinata-backup-", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(wrapper, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @staticmethod
    def import_encrypted_json(source: Path, passphrase: str) -> dict[str, Any]:
        raw = json.loads(source.expanduser().resolve().read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("format") != "hinata-encrypted-backup":
            raise ProtectionError("File is not a valid HINATA encrypted backup")
        plaintext = decrypt_envelope(
            str(raw.get("envelope", "")),
            passphrase.encode("utf-8"),
            aad=b"hinata-backup-v1",
        )
        decoded = json.loads(plaintext.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ProtectionError("Decrypted backup payload is not a JSON object")
        return decoded


def protect_bytes(data: bytes) -> bytes:
    return _crypt(data, protect=True) if os.name == "nt" else data


def unprotect_bytes(data: bytes) -> bytes:
    return _crypt(data, protect=False) if os.name == "nt" else data


def _crypt(data: bytes, *, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    windll = ctypes.windll  # type: ignore[attr-defined]  # Windows-only DPAPI boundary
    input_buffer = ctypes.create_string_buffer(data)
    input_blob = DataBlob(
        len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte))
    )
    output_blob = DataBlob()
    if protect:
        ok = windll.crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            ctypes.c_wchar_p("HINATA owner voice profile"),
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        )
    else:
        ok = windll.crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
        )
    if not ok:
        operation = "protect" if protect else "decrypt"
        raise ProtectionError(f"Windows DPAPI could not {operation} the local voice profile")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        windll.kernel32.LocalFree(output_blob.pbData)


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    restrict_private_path(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(protect_bytes(data))
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)
    restrict_private_path(path)


def _create_private_key_if_absent(path: Path, key: bytes) -> bool:
    """Atomically create a 0600/DPAPI-protected key without replacing another.

    ``write_private`` deliberately overwrites for key rotation. Two processes
    creating the *initial* vault key must instead converge on one file: the old
    replace-based path could silently lose the key used to encrypt existing data.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    restrict_private_path(path.parent)
    payload = protect_bytes(key)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while creating the local vault key")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    if os.name != "nt":
        path.chmod(0o600)
    restrict_private_path(path)
    return True


def read_private(path: Path) -> bytes:
    return unprotect_bytes(path.read_bytes())
