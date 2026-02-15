"""
Client pool manager — maintains a pool of MetaAIClient instances with
warmup, acquire/release, health checks, and proactive session refresh.
"""

import asyncio
import time
from meta_client import MetaAIClient

HEALTH_CHECK_INTERVAL = 60  # seconds
STAGGER_DELAY = 5  # seconds between warmup batches
WARMUP_BATCH_SIZE = 2  # clients initialized in parallel per batch
DEFAULT_QUEUE_TIMEOUT = 120  # seconds
SESSION_MAX_AGE = 10 * 60  # 10 minutes


class ClientPool:
    def __init__(
        self,
        pool_size: int = 15,
        queue_timeout: float = DEFAULT_QUEUE_TIMEOUT,
        proxy: str | None = None,
    ):
        self.pool_size = pool_size
        self.queue_timeout = queue_timeout
        self.proxy = proxy
        self.entries: list[dict] = []
        self._health_check_task: asyncio.Task | None = None
        self._wait_queue: asyncio.Queue = asyncio.Queue()
        self._completed_count = 0
        self._queued_count = 0

        for i in range(pool_size):
            self.entries.append(
                {
                    "id": i,
                    "client": MetaAIClient(proxy=self.proxy, client_id=i),
                    "busy": False,
                    "ready": False,
                    "initializing": False,
                    "session_created_at": None,
                    "release_event": asyncio.Event(),
                }
            )

    async def warmup(self):
        """Initialize clients in parallel batches with a stagger delay between batches."""
        print(
            f"[pool] Warming up {self.pool_size} clients "
            f"(batches of {WARMUP_BATCH_SIZE})..."
        )
        start_time = time.time()
        ready_count = 0

        for batch_start in range(0, len(self.entries), WARMUP_BATCH_SIZE):
            batch_end = min(batch_start + WARMUP_BATCH_SIZE, len(self.entries))
            batch = self.entries[batch_start:batch_end]
            batch_num = batch_start // WARMUP_BATCH_SIZE + 1
            total_batches = (len(self.entries) + WARMUP_BATCH_SIZE - 1) // WARMUP_BATCH_SIZE

            print(
                f"[pool] Batch {batch_num}/{total_batches}: "
                f"initializing clients {batch_start}-{batch_end - 1}"
            )

            async def init_client(entry):
                nonlocal ready_count
                entry["initializing"] = True
                try:
                    await entry["client"].ensure_session()
                    entry["ready"] = True
                    entry["session_created_at"] = time.time()
                    ready_count += 1
                    print(
                        f"[pool] Client {entry['id']} ready "
                        f"({ready_count}/{self.pool_size})"
                    )
                except Exception as err:
                    print(f"[pool] Client {entry['id']} warmup failed: {err}")
                    entry["ready"] = False
                finally:
                    entry["initializing"] = False

            await asyncio.gather(
                *[init_client(entry) for entry in batch],
                return_exceptions=True,
            )

            # Stagger delay between batches (skip after last batch)
            if batch_end < len(self.entries):
                await asyncio.sleep(STAGGER_DELAY)

        if ready_count == 0:
            raise Exception("[pool] No clients could be initialized")

        elapsed = time.time() - start_time
        print(
            f"[pool] Warmup complete: {ready_count}/{self.pool_size} "
            f"clients ready in {elapsed:.1f}s"
        )

        # Start background health check
        self._health_check_task = asyncio.create_task(self._health_check_loop())

    async def acquire(self, timeout: float | None = None) -> dict:
        """
        Acquire a free (ready + not busy) client.
        If none available, wait with a timeout.
        """
        timeout = timeout or self.queue_timeout

        # Try to find a free client immediately
        for entry in self.entries:
            if entry["ready"] and not entry["busy"]:
                entry["busy"] = True
                return entry

        # No free client — wait for one to be released
        ready_count = sum(1 for e in self.entries if e["ready"])
        if ready_count == 0:
            raise Exception("[pool] No healthy clients available")

        self._queued_count += 1
        if self._queued_count % 10 == 1 or self._queued_count <= 3:
            print(
                f"[pool] All {ready_count} clients busy, "
                f"waiting for release..."
            )

        # Wait for any client to become free
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise Exception(
                    f"[pool] Queue timeout after {timeout}s — all clients busy"
                )

            # Check again for a free client
            for entry in self.entries:
                if entry["ready"] and not entry["busy"]:
                    entry["busy"] = True
                    return entry

            # Wait a short interval and retry
            await asyncio.sleep(0.1)

    def release(self, entry: dict):
        """Release a client back to the pool."""
        entry["busy"] = False

        self._completed_count += 1
        if self._completed_count % 10 == 0:
            busy = sum(1 for e in self.entries if e["busy"])
            print(
                f"[pool] Progress: {self._completed_count} completed, "
                f"{busy} busy"
            )

    async def execute(self, fn, timeout: float | None = None):
        """
        Execute an async function with an acquired client.
        One request per client at a time.
        """
        entry = await self.acquire(timeout)
        try:
            result = await fn(entry["client"])
            return result
        except Exception as err:
            # If it looks like a session/auth error, mark client as unhealthy
            msg = str(err)
            if any(
                keyword in msg
                for keyword in [
                    "access token",
                    "LSD token",
                    "401",
                    "403",
                    "session",
                    "exhausted",
                ]
            ):
                print(
                    f"[pool] Client {entry['id']} session error, marking unhealthy"
                )
                entry["ready"] = False
                entry["session_created_at"] = None
                entry["client"].reset_session()
            raise
        finally:
            self.release(entry)

    async def _health_check_loop(self):
        """Run health checks periodically."""
        while True:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            try:
                await self._health_check()
            except Exception as err:
                print(f"[pool] Health check error: {err}")

    async def _health_check(self):
        """
        Background health check:
        1. Re-initialize any failed clients
        2. Proactively refresh idle clients with stale sessions (>10 min)
        """
        # Re-init unhealthy clients
        unhealthy = [
            e
            for e in self.entries
            if not e["ready"] and not e["initializing"] and not e["busy"]
        ]

        if unhealthy:
            print(f"[pool] Health check: {len(unhealthy)} client(s) need re-init")
            for entry in unhealthy:
                try:
                    entry["initializing"] = True
                    entry["client"].reset_session()
                    await entry["client"].ensure_session()
                    entry["ready"] = True
                    entry["session_created_at"] = time.time()
                    print(f"[pool] Client {entry['id']} re-initialized")
                except Exception as err:
                    print(f"[pool] Client {entry['id']} re-init failed: {err}")
                finally:
                    entry["initializing"] = False

        # Proactive token refresh for stale sessions
        now = time.time()
        stale = [
            e
            for e in self.entries
            if e["ready"]
            and not e["busy"]
            and not e["initializing"]
            and e["session_created_at"]
            and now - e["session_created_at"] > SESSION_MAX_AGE
        ]

        if stale:
            print(
                f"[pool] Proactive refresh: {len(stale)} client(s) with stale sessions"
            )
            for entry in stale:
                if entry["busy"]:
                    continue
                try:
                    entry["initializing"] = True
                    entry["busy"] = True
                    entry["client"].reset_session()
                    await entry["client"].ensure_session()
                    entry["session_created_at"] = time.time()
                    print(f"[pool] Client {entry['id']} session refreshed")
                except Exception as err:
                    print(f"[pool] Client {entry['id']} refresh failed: {err}")
                    entry["ready"] = False
                    entry["session_created_at"] = None
                finally:
                    entry["initializing"] = False
                    entry["busy"] = False

    async def shutdown(self):
        """Stop the health check and close all client sessions."""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            self._health_check_task = None

        ready = sum(1 for e in self.entries if e["ready"])
        print(
            f"[pool] Shutdown. {ready}/{self.pool_size} clients were healthy. "
            f"{self._completed_count} requests served."
        )

        # Close all client sessions
        for entry in self.entries:
            try:
                await entry["client"].close()
            except Exception:
                pass
