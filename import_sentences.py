"""
import_sentences.py
───────────────────
Import sentences from Excel into the bot DB.

Expected columns (by name, header row required):
  sentence_id     – original ID from your file (optional, for reference)
  text            – original text (shown to user)
  normalized_text – what the user reads aloud (recorded against)

Usage:
    python import_sentences.py sentences.xlsx
    python import_sentences.py sentences.xlsx --sheet "Sheet2"
    python import_sentences.py sentences.xlsx --dry-run
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    import openpyxl
except ImportError:
    print("❌  Run:  pip install openpyxl")
    sys.exit(1)

DB_PATH = Path("data/recordings.db")


def get_con():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def ensure_table():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_con() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS sentences (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id       INTEGER,
                text            TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                added_at        TEXT NOT NULL,
                is_active       INTEGER NOT NULL DEFAULT 1,
                UNIQUE(text, normalized_text)
            );

            -- add normalized_text column if upgrading from old schema
            -- (safe to run even if column already exists via the UNIQUE constraint above)
        """)
        # Migrate: add columns if they don't exist yet
        cols = {r[1] for r in con.execute("PRAGMA table_info(sentences)").fetchall()}
        if "normalized_text" not in cols:
            con.execute("ALTER TABLE sentences ADD COLUMN normalized_text TEXT NOT NULL DEFAULT ''")
        if "source_id" not in cols:
            con.execute("ALTER TABLE sentences ADD COLUMN source_id INTEGER")


def read_excel(path: str, sheet: str | None) -> list[dict]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        print("❌  Empty sheet."); sys.exit(1)

    # Detect header row
    header = [str(h).strip().lower() if h else "" for h in rows[0]]
    required = {"text", "normalized_text"}
    if not required.issubset(set(header)):
        print(f"❌  Header row must contain columns: {required}")
        print(f"    Found: {header}")
        sys.exit(1)

    col = {name: idx for idx, name in enumerate(header)}
    sid_col = col.get("sentence_id")

    records = []
    for row in rows[1:]:
        text  = str(row[col["text"]]).strip()           if row[col["text"]]            else ""
        norm  = str(row[col["normalized_text"]]).strip() if row[col["normalized_text"]] else ""
        sid   = row[sid_col] if sid_col is not None else None
        if text and norm:
            records.append({"source_id": sid, "text": text, "normalized_text": norm})

    wb.close()
    return records


def import_records(records: list[dict], dry_run: bool) -> dict:
    now     = datetime.utcnow().isoformat()
    added   = 0
    skipped = 0
    dupes   = 0

    seen = set()
    unique = []
    for r in records:
        key = (r["text"].lower(), r["normalized_text"].lower())
        if key in seen:
            dupes += 1
        else:
            seen.add(key)
            unique.append(r)

    if dry_run:
        print(f"\n📋  DRY RUN — nothing will be written\n")
        for i, r in enumerate(unique[:5], 1):
            print(f"  {i}. TEXT:       {r['text'][:80]}")
            print(f"     NORMALIZED: {r['normalized_text'][:80]}\n")
        if len(unique) > 5:
            print(f"  … and {len(unique)-5} more")
        return {"total": len(records), "unique": len(unique), "dupes_in_file": dupes, "added": 0, "skipped": 0}

    with get_con() as con:
        for r in unique:
            try:
                con.execute(
                    "INSERT INTO sentences (source_id,text,normalized_text,added_at) VALUES (?,?,?,?)",
                    (r["source_id"], r["text"], r["normalized_text"], now)
                )
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1

    return {"total": len(records), "unique": len(unique), "dupes_in_file": dupes, "added": added, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", nargs="?", default=None)
    parser.add_argument("--sheet", "-s", default=None)
    parser.add_argument("--dry-run", "-d", action="store_true")
    args = parser.parse_args()

    # If no file passed (e.g. run directly from PyCharm), ask interactively
    if not args.file:
        args.file = input("📂 Paste path to your Excel file: ").strip().strip('"').strip("'")

    if not Path(args.file).exists():
        print(f"❌  File not found: {args.file}"); sys.exit(1)

    ensure_table()
    print(f"📖  Reading '{args.file}' …")
    records = read_excel(args.file, args.sheet)
    print(f"    Found {len(records)} valid rows.")

    result = import_records(records, args.dry_run)

    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Import {"(DRY RUN) " if args.dry_run else ""}complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Rows in file       : {result['total']}
  Unique pairs       : {result['unique']}
  Duplicates in file : {result['dupes_in_file']}
  ✅ Added to DB     : {result['added']}
  ⏭  Already in DB   : {result['skipped']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""")

    if not args.dry_run:
        with get_con() as con:
            total_db = con.execute("SELECT COUNT(*) FROM sentences WHERE is_active=1").fetchone()[0]
        print(f"\n  Total sentences in DB: {total_db}")


if __name__ == "__main__":
    main()