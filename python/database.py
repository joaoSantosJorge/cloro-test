"""
SQLite persistence layer — logs every response (success or failure)
for post-run analysis of success rates, latencies, and failure modes.
"""

import json
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "meta-ai.db"

_db: sqlite3.Connection | None = None


def init_db() -> sqlite3.Connection:
    """
    Initialize the SQLite database. Creates the data/ directory and
    responses table if they don't exist.
    """
    global _db

    os.makedirs(DB_PATH.parent, exist_ok=True)

    _db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    _db.row_factory = sqlite3.Row
    _db.execute("PRAGMA journal_mode=WAL")

    _db.execute(
        """
        CREATE TABLE IF NOT EXISTS responses (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,
            duration_ms INTEGER,
            retried INTEGER DEFAULT 0,
            prompt TEXT,
            country TEXT DEFAULT 'US',
            status_code INTEGER,
            success INTEGER,
            text_length INTEGER DEFAULT 0,
            source_count INTEGER DEFAULT 0,
            model TEXT,
            result_json TEXT
        )
    """
    )
    _db.commit()

    print(f"[db] SQLite database ready at {DB_PATH}")
    return _db


def save_response(meta: dict, result: dict):
    """
    Save a response row.

    Args:
        meta: dict with keys id, timestamp, timestampUnix, durationMs,
              retried, prompt, country, statusCode
        result: The full API result payload
    """
    if _db is None:
        raise RuntimeError("Database not initialized — call init_db() first")

    _db.execute(
        """
        INSERT INTO responses (
            id, timestamp, timestamp_unix, duration_ms, retried,
            prompt, country, status_code, success,
            text_length, source_count, model, result_json
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?
        )
    """,
        (
            meta["id"],
            meta["timestamp"],
            meta["timestamp_unix"],
            meta["duration_ms"],
            1 if meta.get("retried") else 0,
            meta.get("prompt", ""),
            meta.get("country", "US"),
            meta["status_code"],
            1 if result.get("success") else 0,
            len((result.get("result") or {}).get("text", "")),
            len((result.get("result") or {}).get("sources", [])),
            (result.get("result") or {}).get("model"),
            json.dumps(result),
        ),
    )
    _db.commit()


def get_response(response_id: str) -> dict | None:
    """Fetch a response by id."""
    if _db is None:
        raise RuntimeError("Database not initialized — call init_db() first")

    row = _db.execute(
        "SELECT * FROM responses WHERE id = ?", (response_id,)
    ).fetchone()

    if row is None:
        return None

    result = dict(row)
    if result.get("result_json"):
        result["result"] = json.loads(result["result_json"])
    return result
