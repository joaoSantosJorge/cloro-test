"""
Parallel test script — fires N concurrent POST requests to the Meta AI scraper.

Environment variables:
    CONCURRENCY   — number of parallel requests (default: 100)
    TEST_PROMPT   — the prompt to send (default: a generic test prompt)
    PORT          — server port (default: 8000)
    API_KEY       — optional Bearer token
    MAX_IN_FLIGHT — max concurrent requests (default: 50)
"""

import asyncio
import os
import time
import json
import statistics

from dotenv import load_dotenv
import httpx

load_dotenv(override=True)

CONCURRENCY = int(os.getenv("CONCURRENCY", "100"))
MAX_IN_FLIGHT = int(os.getenv("MAX_IN_FLIGHT", "50"))
TEST_PROMPT = os.getenv("TEST_PROMPT", "What are the latest developments in AI?")
PORT = int(os.getenv("PORT", "8000"))
API_KEY = os.getenv("API_KEY", "sk_live_test")
BASE_URL = f"http://localhost:{PORT}"

completed = 0
pad_width = len(str(CONCURRENCY))


async def send_request(client: httpx.AsyncClient, index: int, semaphore: asyncio.Semaphore) -> dict:
    global completed
    async with semaphore:
        start = time.time()
        try:
            resp = await client.post(
                f"{BASE_URL}/v1/monitor/meta-ai",
                json={
                    "prompt": f"{TEST_PROMPT} (request #{index + 1})",
                    "country": "US",
                    "include": {"markdown": True},
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {API_KEY}",
                },
                timeout=300,
            )

            data = resp.json()
            duration_ms = int((time.time() - start) * 1000)
            text_len = len((data.get("result") or {}).get("text", ""))
            source_count = len((data.get("result") or {}).get("sources", []))

            completed += 1
            status = "OK" if data.get("success") else "FAIL"
            detail = (
                f"text={text_len} sources={source_count}"
                if data.get("success")
                else f'error="{data.get("error", "")}"'
            )
            print(
                f"[{completed:>{pad_width}}/{CONCURRENCY}] "
                f"#{index + 1:>{pad_width}} {status} {duration_ms}ms {detail}"
            )

            return {
                "index": index + 1,
                "status": resp.status_code,
                "success": data.get("success", False),
                "duration_ms": duration_ms,
                "text_len": text_len,
                "source_count": source_count,
                "error": data.get("error"),
            }

        except Exception as err:
            duration_ms = int((time.time() - start) * 1000)
            completed += 1
            print(
                f"[{completed:>{pad_width}}/{CONCURRENCY}] "
                f"#{index + 1:>{pad_width}} ERR {duration_ms}ms "
                f'error="{err}"'
            )
            return {
                "index": index + 1,
                "status": 0,
                "success": False,
                "duration_ms": duration_ms,
                "text_len": 0,
                "source_count": 0,
                "error": str(err),
            }


def percentile(sorted_list: list, p: float) -> float:
    idx = max(0, int((p / 100) * len(sorted_list)) - 1)
    return sorted_list[idx]


async def main():
    print(f"\n=== Parallel Test ===")
    print(f"Total requests: {CONCURRENCY}")
    print(f"Max in-flight: {MAX_IN_FLIGHT}")
    print(f'Prompt: "{TEST_PROMPT}"')
    print(f"Server: {BASE_URL}")
    print(f"\nFiring {CONCURRENCY} requests (max {MAX_IN_FLIGHT} in-flight)...\n")

    overall_start = time.time()
    semaphore = asyncio.Semaphore(MAX_IN_FLIGHT)

    async with httpx.AsyncClient() as client:
        tasks = [send_request(client, i, semaphore) for i in range(CONCURRENCY)]
        results = await asyncio.gather(*tasks)

    overall_duration = int((time.time() - overall_start) * 1000)

    # Compute latency percentiles
    durations = sorted(r["duration_ms"] for r in results)
    p50 = percentile(durations, 50)
    p95 = percentile(durations, 95)
    p99 = percentile(durations, 99)

    succeeded = sum(1 for r in results if r["success"])
    failed = sum(1 for r in results if not r["success"])
    avg_duration = int(statistics.mean(r["duration_ms"] for r in results))

    print(f"\n--- Summary ---")
    print(f"  Total:     {CONCURRENCY} requests")
    print(f"  Succeeded: {succeeded} ({succeeded / CONCURRENCY * 100:.1f}%)")
    print(f"  Failed:    {failed}")
    print(f"  Wall time: {overall_duration}ms ({overall_duration / 1000:.1f}s)")
    print(f"  Avg:       {avg_duration}ms")
    print(f"  Min:       {durations[0]}ms")
    print(f"  p50:       {p50}ms")
    print(f"  p95:       {p95}ms")
    print(f"  p99:       {p99}ms")
    print(f"  Max:       {durations[-1]}ms")
    print()

    exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    asyncio.run(main())
