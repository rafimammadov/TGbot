"""
Import sentences from an Excel file into the bot DB.

Expected columns (any order, case-insensitive):
    sentence_id | text | normalized_text

Usage:
    python import_sentences.py sentences.xlsx
    python import_sentences.py sentences.xlsx --sheet "Sheet2"
    python import_sentences.py sentences.xlsx --dry-run
"""

import sys, sqlite3, argparse
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd

DB_PATH = Path("data/recordings.db")

def get_con():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file",       help="Path to .xlsx file")
    parser.add_argument("--sheet",    default=0, help="Sheet name or index (default: first sheet)")
    parser.add_argument("--dry-run",  action="store_true", help="Preview without inserting")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"❌ File not found: {path}")
        sys.exit(1)

    # Read Excel
    try:
        df = pd.read_excel(path, sheet_name=args.sheet, dtype=str)
    except Exception as e:
        print(f"❌ Failed to read Excel: {e}")
        sys.exit(1)

    # Normalize column names
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    missing = [c for c in ("text", "normalized_text") if c not in df.columns]
    if missing:
        print(f"❌ Missing required columns: {missing}")
        print(f"   Found columns: {list(df.columns)}")
        sys.exit(1)

    df = df.dropna(subset=["text", "normalized_text"])
    df["text"]            = df["text"].str.strip()
    df["normalized_text"] = df["normalized_text"].str.strip()
    df = df[(df["text"] != "") & (df["normalized_text"] != "")]

    # Use sentence_id from file if present, else ignore
    has_source_id = "sentence_id" in df.columns

    print(f"📄 File:    {path.name}")
    print(f"📊 Rows:    {len(df)}")
    print(f"🔑 Columns: {list(df.columns)}")

    if args.dry_run:
        print("\n--- DRY RUN (first 5 rows) ---")
        print(df[["text", "normalized_text"]].head().to_string(index=False))
        print("\nNo changes made.")
        return

    if not DB_PATH.exists():
        print(f"❌ DB not found at {DB_PATH}. Run bot.py once to initialise it.")
        sys.exit(1)

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    inserted = skipped = errors = 0

    with get_con() as con:
        for _, row in df.iterrows():
            text  = row["text"]
            norm  = row["normalized_text"]
            src   = int(row["sentence_id"]) if has_source_id and pd.notna(row.get("sentence_id")) else None
            try:
                con.execute(
                    "INSERT OR IGNORE INTO sentences (source_id, text, normalized_text, added_at) VALUES (?,?,?,?)",
                    (src, text, norm, now)
                )
                if con.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                print(f"  ⚠️  Row skipped ({e}): {text[:60]}")
                errors += 1

    print(f"\n✅ Done.")
    print(f"   Inserted : {inserted}")
    print(f"   Skipped  : {skipped}  (duplicates)")
    print(f"   Errors   : {errors}")

if __name__ == "__main__":
    main()