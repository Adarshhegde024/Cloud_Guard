#!/usr/bin/env python3
"""
CloudGuard — SQLite to Supabase Migration & Sync Script
Reads all tables from the local SQLite database and uploads them to Supabase.
"""

import os
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.append(str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

import supabase_sync

TABLES = [
    ("users", "id"),
    ("sessions", "id"),
    ("invitations", "id"),
    ("guest_sessions", "id"),
    ("documents", "id"),
    ("recipients", "id"),
    ("document_shares", None),
    ("recipient_accounts", "id"),
    ("recipient_sessions", "id"),
    ("nominees", "id"),
    ("nominee_accounts", "id"),
    ("nominee_sessions", "id"),
    ("reviewer_sessions", "id"),
    ("death_cases", "id"),
    ("nominee_case_events", "id"),
    ("medical_holds", "owner_id"),
    ("document_releases", None),
    ("recipient_events", "id"),
    ("checkins", "id"),
    ("audit_logs", "id"),
]

def main():
    print("=" * 60)
    print("CloudGuard — Supabase Data Sync Utility")
    print("=" * 60)
    print(f"Supabase URL: {supabase_sync.SUPABASE_URL}")
    print(f"Checking Supabase connection...")

    conn_check = supabase_sync.test_supabase_connection()
    if not conn_check.get("connected"):
        print(f"ERROR: Cannot connect to Supabase: {conn_check}")
        sys.exit(1)
    print("Supabase connection verified!\n")

    db_path = BASE_DIR / "cloudguard.db"
    if not db_path.exists():
        print(f"Database file not found at {db_path}. Nothing to sync.")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    for table, pkey in TABLES:
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.OperationalError:
            print(f"[-] Table '{table}' does not exist in local SQLite, skipping.")
            continue

        if not rows:
            print(f"[*] Table '{table}': 0 rows (skipped)")
            continue

        success_count = 0
        fail_count = 0
        for r in rows:
            data = dict(r)
            ok = supabase_sync.sync_record_sync(table, data, on_conflict=pkey)
            if ok:
                success_count += 1
            else:
                fail_count += 1

        print(f"[+] Table '{table}': Synced {success_count}/{len(rows)} records (Failures: {fail_count})")

    conn.close()
    print("\nSync complete! If any tables failed, ensure you ran 'supabase_schema.sql' in the Supabase SQL Editor.")

if __name__ == "__main__":
    main()
