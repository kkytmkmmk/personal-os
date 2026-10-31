"""Apply a private JSON bundle fetched with the connected Money Forward ME plugin."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from personal_os.moneyforward_sync import import_snapshot, MAX_BYTES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--backup", action="store_true", help="Create and verify a full backup before applying")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > MAX_BYTES:
            raise ValueError("更新JSONは8MB以内にしてください。")
        bundle = json.loads(args.input.read_text(encoding="utf-8-sig"))
        if args.backup:
            if not app.DB_PATH.is_file():
                raise ValueError("バックアップ対象のPersonal OS DBがありません。")
            backup = app.backup_database(force=True)
            if not app.verify_backup(app._backup_display_path(backup))["valid"]:
                raise ValueError("バックアップの検証に失敗しました。")
        print(json.dumps(import_snapshot(app.ATTACHMENT_DIR / "integrations" / "moneyforward", bundle), ensure_ascii=False))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
