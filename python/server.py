"""
FastAPI server — single POST endpoint for Meta AI scraping with
client pool, retry logic, and SQLite persistence.
"""

import os
import time
import secrets
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from client_pool import ClientPool
from database import init_db, save_response

load_dotenv(override=True)

POOL_SIZE = int(os.getenv("POOL_SIZE", "15"))
QUEUE_TIMEOUT = int(os.getenv("QUEUE_TIMEOUT", "120000")) / 1000  # convert ms to s
PROXY_URL = os.getenv("PROXY_URL") or None
PORT = int(os.getenv("PORT", "8000"))

pool: ClientPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and warm up pool on startup, shutdown on exit."""
    global pool
    init_db()
    pool = ClientPool(pool_size=POOL_SIZE, queue_timeout=QUEUE_TIMEOUT, proxy=PROXY_URL)
    await pool.warmup()

    proxy_display = PROXY_URL.split("@")[-1] if PROXY_URL and "@" in PROXY_URL else (PROXY_URL or "none (direct)")
    print(f"[server] Meta AI scraper running on http://localhost:{PORT}")
    print(f"[server] Pool size: {POOL_SIZE}, Queue timeout: {QUEUE_TIMEOUT}s")
    print(f"[server] Proxy: {proxy_display}")

    yield

    if pool:
        await pool.shutdown()


app = FastAPI(lifespan=lifespan)


@app.post("/v1/monitor/meta-ai")
async def monitor_meta_ai(request: Request):
    start_time = time.time()
    retried = False

    try:
        body = await request.json()
        prompt = body.get("prompt")
        country = body.get("country", "US")
        include = body.get("include", {})

        if not prompt:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "prompt is required"},
            )

        print(
            f'[server] Received prompt: "{prompt[:50]}..." (country={country})'
        )

        result = None
        try:
            result = await pool.execute(lambda client: client.prompt(prompt))
        except Exception as err:
            # Retry once via pool (may pick a different healthy client)
            print(f"[server] First attempt failed: {err}")
            retried = True
            result = await pool.execute(lambda client: client.prompt(prompt))

        # If markdown not requested, strip html/markdown fields
        if not include.get("markdown"):
            result.get("result", {}).pop("html", None)
            result.get("result", {}).pop("markdown", None)

        now = datetime.now(timezone.utc)
        response_id = secrets.token_hex(4)
        duration_ms = int((time.time() - start_time) * 1000)

        save_response(
            {
                "id": response_id,
                "timestamp": now.isoformat(),
                "timestamp_unix": int(now.timestamp() * 1000),
                "duration_ms": duration_ms,
                "retried": retried,
                "prompt": prompt,
                "country": country,
                "status_code": 200,
            },
            result,
        )

        return JSONResponse(content=result)

    except Exception as err:
        print(f"[server] Error: {err}")
        error_result = {"success": False, "error": str(err)}

        now = datetime.now(timezone.utc)
        response_id = secrets.token_hex(4)
        duration_ms = int((time.time() - start_time) * 1000)

        try:
            body = await request.json()
        except Exception:
            body = {}

        save_response(
            {
                "id": response_id,
                "timestamp": now.isoformat(),
                "timestamp_unix": int(now.timestamp() * 1000),
                "duration_ms": duration_ms,
                "retried": retried,
                "prompt": body.get("prompt", ""),
                "country": body.get("country", "US"),
                "status_code": 502,
            },
            error_result,
        )

        return JSONResponse(status_code=502, content=error_result)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=PORT, log_level="info")
