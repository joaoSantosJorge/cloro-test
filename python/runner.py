"""
Batch runner for Meta AI scraper.

Sends TOTAL_REQUESTS parallel prompts through Decodo rotating proxies,
writing every result to SQLite via a serialized db_writer coroutine.
"""

import asyncio
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

from meta_client import MetaAIClient

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

PROXY_URL = os.getenv("PROXY_URL", "")
PROMPT = os.getenv("PROMPT", "What do you know about Tesla's latest updates?")
TOTAL_REQUESTS = int(os.getenv("TOTAL_REQUESTS", "1000"))
PARALLEL_REQUESTS = int(os.getenv("PARALLEL_REQUESTS", "15"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "meta-ai.db"

# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------


def make_session_proxy(base_url: str, session_id: str, country: str = "us") -> str:
    """Build a session-pinned Decodo proxy URL."""
    parsed = urlparse(base_url)
    new_username = f"user-{parsed.username}-country-{country}-session-{session_id}"
    netloc = f"{new_username}:{parsed.password}@{parsed.hostname}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def init_db() -> sqlite3.Connection:
    """Create (or recreate) the responses table and return a connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DROP TABLE IF EXISTS responses")
    conn.execute(
        """
        CREATE TABLE responses (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,
            duration_ms INTEGER,
            prompt TEXT,
            country TEXT DEFAULT 'US',
            success INTEGER,
            text_length INTEGER DEFAULT 0,
            source_count INTEGER DEFAULT 0,
            model TEXT,
            result_json TEXT
        )
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


async def run_single(
    index: int,
    semaphore: asyncio.Semaphore,
    queue: asyncio.Queue,
) -> None:
    """Execute a single Meta AI request, retrying with a fresh proxy IP on failure."""
    async with semaphore:
        row_id = str(uuid.uuid4())
        ts = time.time()
        ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        tagged_prompt = PROMPT
        start = time.monotonic()

        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            session_id = uuid.uuid4().hex
            proxy_url = make_session_proxy(PROXY_URL, session_id) if PROXY_URL else None
            client = MetaAIClient(proxy=proxy_url, client_id=index)

            try:
                result = await client.prompt(tagged_prompt)
                duration_ms = int((time.monotonic() - start) * 1000)

                text = result.get("result", {}).get("text", "")
                sources = result.get("result", {}).get("sources", [])
                model = result.get("result", {}).get("model", "")

                # Validate response quality — retry on empty or useless answers
                if not text.strip():
                    raise Exception("Empty response from Meta AI")
                if len(text) < 200 and not sources:
                    raise Exception(f"Low-quality response ({len(text)} chars, no sources): {text[:100]}")

                await queue.put(
                    {
                        "id": row_id,
                        "timestamp": ts_iso,
                        "timestamp_unix": int(ts),
                        "duration_ms": duration_ms,
                        "prompt": PROMPT,
                        "country": "US",
                        "success": 1,
                        "text_length": len(text),
                        "source_count": len(sources),
                        "model": model,
                        "result_json": json.dumps(result, ensure_ascii=False),
                    }
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    print(f"[runner] #{index} retry {attempt + 1}/{MAX_RETRIES}")
            finally:
                await client.close()

        # All retries exhausted
        duration_ms = int((time.monotonic() - start) * 1000)
        await queue.put(
            {
                "id": row_id,
                "timestamp": ts_iso,
                "timestamp_unix": int(ts),
                "duration_ms": duration_ms,
                "prompt": PROMPT,
                "country": "US",
                "success": 0,
                "text_length": 0,
                "source_count": 0,
                "model": "",
                "result_json": json.dumps(
                    {"error": str(last_error)}, ensure_ascii=False
                ),
            }
        )


async def db_writer(
    conn: sqlite3.Connection,
    queue: asyncio.Queue,
    total: int,
) -> None:
    """Consume results from the queue and write them to SQLite."""
    done = 0
    ok = 0
    fail = 0

    while done < total:
        row = await queue.get()
        conn.execute(
            """
            INSERT INTO responses
                (id, timestamp, timestamp_unix, duration_ms, prompt, country,
                 success, text_length, source_count, model, result_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["timestamp"],
                row["timestamp_unix"],
                row["duration_ms"],
                row["prompt"],
                row["country"],
                row["success"],
                row["text_length"],
                row["source_count"],
                row["model"],
                row["result_json"],
            ),
        )
        conn.commit()

        done += 1
        if row["success"]:
            ok += 1
        else:
            fail += 1

        rate = (ok / done * 100) if done else 0
        print(
            f"[runner] {done}/{total} done | OK: {ok} FAIL: {fail} | "
            f"rate: {rate:.1f}% | last: {row['duration_ms']}ms"
        )

    return ok, fail


async def main() -> None:
    print(f"[runner] Starting batch: {TOTAL_REQUESTS} requests, {PARALLEL_REQUESTS} parallel")
    print(f"[runner] Prompt: {PROMPT[:80]}{'...' if len(PROMPT) > 80 else ''}")
    print(f"[runner] DB: {DB_PATH}")

    conn = init_db()
    semaphore = asyncio.Semaphore(PARALLEL_REQUESTS)
    queue: asyncio.Queue = asyncio.Queue()

    writer_task = asyncio.create_task(db_writer(conn, queue, TOTAL_REQUESTS))

    tasks = [
        asyncio.create_task(run_single(i, semaphore, queue))
        for i in range(TOTAL_REQUESTS)
    ]

    await asyncio.gather(*tasks)
    ok, fail = await writer_task

    conn.close()

    total = ok + fail
    rate = (ok / total * 100) if total else 0
    print(f"\n[runner] COMPLETE: {total} requests")
    print(f"[runner] Success: {ok} ({rate:.1f}%)")
    print(f"[runner] Failed:  {fail}")


if __name__ == "__main__":
    asyncio.run(main())
