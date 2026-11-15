"""Offline Asset Manager & SHA-256 verified bundle import/export for HINATA.

Tracks local model weights, STT/TTS voice packs, wake-word artifacts, and
runtime dependencies in SQLite ``offline_assets`` with cryptographic SHA-256
verification so air-gapped laptops can audit and import pre-downloaded bundles
without touching the internet.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hinata.config import HinataConfig
from hinata.database.database import Database


class AssetVerificationError(RuntimeError):
    """Raised when an offline asset or bundle fails integrity or path safety checks."""


@dataclass(frozen=True, slots=True)
class AssetRecord:
    asset_id: str
    category: str  # llm, embedding, stt, tts, wake_word, bundle_file
    name: str
    version: str = ""
    local_path: str = ""
    expected_sha256: str = ""
    size_bytes: int = 0
    license: str = ""
    source: str = "local"
    status: str = "unknown"  # ready, missing, hash_mismatch, configured_runtime

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


class OfflineAssetManager:
    """Manages local model/speech assets and air-gapped bundle archives."""

    def __init__(self, database: Database, config: HinataConfig) -> None:
        self.database = database
        self.database.ensure_migrated()
        self.config = config

    def register_file_asset(
        self,
        *,
        asset_id: str,
        category: str,
        name: str,
        path: str | Path,
        expected_sha256: str = "",
        version: str = "",
        license_id: str = "",
        source: str = "local",
    ) -> dict[str, Any]:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            status = "missing"
            actual_sha = ""
            size_bytes = 0
        else:
            actual_sha, size_bytes = compute_file_sha256(resolved)
            if expected_sha256 and actual_sha.lower() != expected_sha256.lower():
                status = "hash_mismatch"
            else:
                status = "ready"
                expected_sha256 = expected_sha256 or actual_sha

        self.database.execute(
            """INSERT INTO offline_assets(
                asset_id, category, name, version, local_path, expected_sha256,
                size_bytes, license, source, status, verified_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(asset_id) DO UPDATE SET
                category = excluded.category,
                name = excluded.name,
                version = excluded.version,
                local_path = excluded.local_path,
                expected_sha256 = excluded.expected_sha256,
                size_bytes = excluded.size_bytes,
                license = excluded.license,
                source = excluded.source,
                status = excluded.status,
                verified_at = datetime('now')""",
            (
                asset_id,
                category,
                name,
                version,
                str(resolved),
                expected_sha256.lower(),
                size_bytes,
                license_id,
                source,
                status,
            ),
        )
        if status == "hash_mismatch":
            raise AssetVerificationError(
                f"SHA-256 mismatch for asset '{asset_id}': expected {expected_sha256}, got {actual_sha}"
            )
        return self.get_asset(asset_id) or {}

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        return self.database.fetch_one("SELECT * FROM offline_assets WHERE asset_id = ?", (asset_id,))

    def list_assets(self) -> list[dict[str, Any]]:
        return self.database.fetch_all("SELECT * FROM offline_assets ORDER BY category, asset_id")

    def verify_all(self) -> dict[str, Any]:
        rows = self.list_assets()
        verified: list[str] = []
        missing: list[str] = []
        mismatched: list[str] = []
        for row in rows:
            local_path = str(row.get("local_path", ""))
            if not local_path:
                continue
            p = Path(local_path)
            if not p.is_file():
                missing.append(row["asset_id"])
                self.database.execute(
                    "UPDATE offline_assets SET status='missing', verified_at=datetime('now') WHERE asset_id=?",
                    (row["asset_id"],),
                )
                continue
            sha, sz = compute_file_sha256(p)
            exp = str(row.get("expected_sha256", "")).lower()
            if exp and sha != exp:
                mismatched.append(row["asset_id"])
                self.database.execute(
                    "UPDATE offline_assets SET status='hash_mismatch', verified_at=datetime('now') WHERE asset_id=?",
                    (row["asset_id"],),
                )
            else:
                verified.append(row["asset_id"])
                self.database.execute(
                    "UPDATE offline_assets SET status='ready', size_bytes=?, verified_at=datetime('now') WHERE asset_id=?",
                    (sz, row["asset_id"]),
                )
        return {
            "ready": verified,
            "missing": missing,
            "hash_mismatch": mismatched,
            "all_valid": not missing and not mismatched,
        }

    def readiness_inventory(self, installed_ollama_models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Inspect configured LLM, embedding, STT, TTS, and wake-word offline readiness."""
        ollama_map = {
            str(m.get("name", "")): str(m.get("digest", ""))
            for m in (installed_ollama_models or [])
        }
        speech = self.config.speech
        stt_ready = False
        if speech.stt_backend == "vosk" and speech.vosk_model_path:
            stt_ready = Path(speech.vosk_model_path).expanduser().is_dir()
        elif speech.stt_backend == "faster-whisper" and speech.whisper_model:
            candidate = Path(speech.whisper_model).expanduser()
            stt_ready = candidate.is_dir()

        piper_ready = True
        if speech.tts_backend == "piper":
            piper_ready = bool(
                speech.piper_model and Path(speech.piper_model).expanduser().is_file()
            )

        return {
            "chat_model": {
                "configured": self.config.ollama.model,
                "installed": any(
                    k == self.config.ollama.model or k.startswith(self.config.ollama.model.split(":")[0])
                    for k in ollama_map
                )
                if self.config.ollama.model
                else bool(ollama_map),
            },
            "embedding_model": {
                "configured": self.config.ollama.embedding_model,
                "installed": any(
                    k == self.config.ollama.embedding_model
                    or k.startswith(self.config.ollama.embedding_model.split(":")[0])
                    for k in ollama_map
                ),
            },
            "stt": {
                "backend": speech.stt_backend,
                "local_path_ready": stt_ready,
            },
            "tts": {
                "backend": speech.tts_backend,
                "ready": piper_ready,
            },
            "registered_assets": self.list_assets(),
        }

    def export_bundle(self, bundle_path: str | Path) -> dict[str, Any]:
        """Package registered ready file assets + manifest.json into a portable .tar.gz bundle."""
        out_path = Path(bundle_path).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_entries: list[dict[str, Any]] = []
        with tarfile.open(out_path, "w:gz") as tar:
            for row in self.list_assets():
                lpath = Path(str(row.get("local_path", "")))
                if not lpath.is_file():
                    continue
                sha, sz = compute_file_sha256(lpath)
                rel_arc = f"files/{row['asset_id']}_{lpath.name}"
                tar.add(lpath, arcname=rel_arc, recursive=False)
                manifest_entries.append(
                    {
                        "asset_id": row["asset_id"],
                        "category": row["category"],
                        "name": row["name"],
                        "version": row.get("version", ""),
                        "arcname": rel_arc,
                        "sha256": sha,
                        "size_bytes": sz,
                        "license": row.get("license", ""),
                    }
                )
            manifest_bytes = json.dumps(
                {"bundle_format": 1, "assets": manifest_entries}, indent=2, sort_keys=True
            ).encode("utf-8")
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest_bytes)
            import io
            tar.addfile(info, io.BytesIO(manifest_bytes))
        bundle_sha, bundle_size = compute_file_sha256(out_path)
        return {
            "bundle_path": str(out_path),
            "sha256": bundle_sha,
            "size_bytes": bundle_size,
            "asset_count": len(manifest_entries),
        }

    def import_bundle(self, bundle_path: str | Path, destination_dir: str | Path | None = None) -> dict[str, Any]:
        """Safely extract and SHA-256 verify an offline bundle without path traversal or symlinks."""
        src = Path(bundle_path).expanduser().resolve()
        if not src.is_file():
            raise AssetVerificationError(f"Bundle file not found: {src}")
        dest_root = (
            Path(destination_dir).expanduser().resolve()
            if destination_dir is not None
            else (self.config.data_dir / "assets").resolve()
        )
        dest_root.mkdir(parents=True, exist_ok=True)

        imported: list[str] = []
        with tarfile.open(src, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) > 512:
                raise AssetVerificationError("Offline bundle exceeds member count limit")
            for m in members:
                if m.issym() or m.islnk() or m.ischr() or m.isblk() or m.isfifo():
                    raise AssetVerificationError(f"Unsafe archive entry type in bundle: {m.name}")
                target_check = (dest_root / m.name).resolve()
                try:
                    target_check.relative_to(dest_root)
                except ValueError as exc:
                    raise AssetVerificationError(f"Bundle archive path traversal blocked: {m.name}") from exc

            try:
                manifest_member = tar.getmember("manifest.json")
            except KeyError as exc:
                raise AssetVerificationError("Offline bundle missing manifest.json") from exc
            raw_manifest = tar.extractfile(manifest_member)
            if raw_manifest is None:
                raise AssetVerificationError("Offline bundle manifest.json is unreadable")
            manifest = json.loads(raw_manifest.read(1_000_000).decode("utf-8"))
            if manifest.get("bundle_format") != 1 or not isinstance(manifest.get("assets"), list):
                raise AssetVerificationError("Unsupported offline bundle manifest format")

            for entry in manifest["assets"]:
                arcname = str(entry["arcname"])
                expected_sha = str(entry["sha256"]).lower()
                member = tar.getmember(arcname)
                extracted_stream = tar.extractfile(member)
                if extracted_stream is None:
                    raise AssetVerificationError(f"Missing bundle payload {arcname}")
                out_file = (dest_root / Path(arcname).name).resolve()
                out_file.relative_to(dest_root)
                tmp_file = out_file.with_suffix(out_file.suffix + ".part")
                with tmp_file.open("wb") as out_handle:
                    shutil.copyfileobj(extracted_stream, out_handle, length=65536)
                actual_sha, _ = compute_file_sha256(tmp_file)
                if actual_sha != expected_sha:
                    tmp_file.unlink(missing_ok=True)
                    raise AssetVerificationError(
                        f"Bundle asset '{entry['asset_id']}' failed SHA-256 verification"
                    )
                tmp_file.replace(out_file)
                self.register_file_asset(
                    asset_id=str(entry["asset_id"]),
                    category=str(entry["category"]),
                    name=str(entry["name"]),
                    path=out_file,
                    expected_sha256=expected_sha,
                    version=str(entry.get("version", "")),
                    license_id=str(entry.get("license", "")),
                    source="offline_bundle",
                )
                imported.append(str(entry["asset_id"]))
        return {"destination": str(dest_root), "imported_assets": imported}
