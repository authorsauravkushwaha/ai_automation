"""Explicit, backup-first migration for currently vault-backed SQLite fields.

Only scheduled_tasks.request_text currently uses LocalVault field envelopes.
Other tables are NOT implicitly encrypted by this migration. SQLCipher databases
must be migrated through their own driver; this utility accepts plaintext SQLite.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from hinata.security.local_protection import ENVELOPE_PREFIX, LocalVault, ProtectionError, _aesgcm


def migrate_schedule_envelopes(database: Path, vault: LocalVault, backup: Path, *,
                               encrypt_plaintext: bool = False) -> dict[str, int | str]:
    _aesgcm()  # Fail before creating artifacts if secure-storage isn't installed.
    if not vault.enabled:
        raise ProtectionError("Enable the vault before migrating fields")
    if not vault.key_path.is_file():
        raise ProtectionError("Existing vault key required; migration never rotates or generates it")
    source = database.resolve(strict=True)
    backup = backup.expanduser().absolute()
    if backup.exists() or backup.is_symlink() or backup.resolve() == source:
        raise ProtectionError("Backup must be a new file, distinct from the database")
    backup.parent.mkdir(parents=True, exist_ok=True)
    uri = source.as_uri() + "?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.execute("BEGIN IMMEDIATE")  # Keep other writers out through snapshot + commit.
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(uri, uri=True)) as snapshot, closing(sqlite3.connect(backup)) as destination:
                snapshot.backup(destination)
            # Windows _commit (os.fsync) requires a writable descriptor.
            with backup.open("r+b") as stream:
                os.fsync(stream.fileno())
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
        changed = 0
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_tasks'").fetchone()
        if table:
            for schedule_id, text in conn.execute("SELECT schedule_id, request_text FROM scheduled_tasks"):
                context = "schedule:" + str(schedule_id)
                if text.startswith("enc:"):
                    plain = vault.unprotect_text(text, context=context)
                    if text.startswith(ENVELOPE_PREFIX):
                        continue
                elif encrypt_plaintext:
                    plain = text
                else:
                    continue
                sealed = vault.protect_text(plain, context=context)
                # Verify every rewritten value before committing any of them.
                if vault.unprotect_text(sealed, context=context) != plain:
                    raise ProtectionError("Migration verification failed")
                conn.execute("UPDATE scheduled_tasks SET request_text=? WHERE schedule_id=?", (sealed, schedule_id))
                changed += 1
        conn.commit()
        return {"migrated_fields": changed, "backup": str(backup), "format": "enc:v2:AES-256-GCM"}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
