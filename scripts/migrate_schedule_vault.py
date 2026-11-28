"""Backup-first owner-invoked schedule encryption migration; never rotates keys."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hinata.security.local_protection import LocalVault  # noqa: E402
from hinata.security.vault_migration import migrate_schedule_envelopes  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path, help="Existing directory containing vault.key")
    parser.add_argument("--backup", required=True, type=Path, help="NEW private SQLite backup filename")
    parser.add_argument("--encrypt-plaintext", action="store_true")
    parser.add_argument("--confirm", action="store_true", help="Confirm the assistant/watchers are stopped and key is backed up")
    args = parser.parse_args()
    if not args.confirm:
        parser.error("Stop HINATA/watchers, back up vault.key, then pass --confirm")
    result = migrate_schedule_envelopes(args.database, LocalVault(args.data_dir, enabled=True),
                                        args.backup, encrypt_plaintext=args.encrypt_plaintext)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
