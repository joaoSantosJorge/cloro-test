const { MetaAIClient } = require("./meta-client");

const HEALTH_CHECK_INTERVAL = 60_000; // 60 seconds
const STAGGER_DELAY = 5_000; // 5 seconds between warmup batches
const WARMUP_BATCH_SIZE = 2; // clients initialized in parallel per batch
const DEFAULT_QUEUE_TIMEOUT = 120_000; // 2 minutes
const SESSION_MAX_AGE = 10 * 60_000; // 10 minutes — refresh before tokens go stale

class ClientPool {
  /**
   * @param {number} poolSize Number of MetaAIClient instances to maintain
   * @param {object} [opts]
   * @param {number} [opts.queueTimeout] Max ms a request waits in queue (default 120s)
   */
  constructor(poolSize = 15, opts = {}) {
    this.poolSize = poolSize;
    this.queueTimeout = opts.queueTimeout || DEFAULT_QUEUE_TIMEOUT;
    this.proxy = opts.proxy || null;
    this.browserProxy = opts.browserProxy || null;
    this.entries = [];
    this.healthCheckTimer = null;
    this._waitQueue = []; // requests waiting for a free client
    this._completedCount = 0; // requests completed since last summary
    this._queuedCount = 0; // total queued (for log throttle)

    for (let i = 0; i < poolSize; i++) {
      this.entries.push({
        id: i,
        client: new MetaAIClient(this.proxy, i, { browserProxy: this.browserProxy }),
        busy: false,
        ready: false,
        initializing: false,
        sessionCreatedAt: null, // track when session was established
      });
    }
  }

  /**
   * Initialize clients in parallel batches with a stagger delay between batches.
   * Much faster than fully sequential for large pools.
   * @returns {Promise<void>}
   */
  async warmup() {
    console.log(`[pool] Warming up ${this.poolSize} clients (batches of ${WARMUP_BATCH_SIZE})...`);
    const startTime = Date.now();
    let readyCount = 0;

    for (let batchStart = 0; batchStart < this.entries.length; batchStart += WARMUP_BATCH_SIZE) {
      const batchEnd = Math.min(batchStart + WARMUP_BATCH_SIZE, this.entries.length);
      const batch = this.entries.slice(batchStart, batchEnd);
      const batchNum = Math.floor(batchStart / WARMUP_BATCH_SIZE) + 1;
      const totalBatches = Math.ceil(this.entries.length / WARMUP_BATCH_SIZE);

      console.log(`[pool] Batch ${batchNum}/${totalBatches}: initializing clients ${batchStart}-${batchEnd - 1}`);

      // Initialize all clients in this batch in parallel
      const results = await Promise.allSettled(
        batch.map(async (entry) => {
          entry.initializing = true;
          try {
            await entry.client.ensureSession();
            entry.ready = true;
            entry.sessionCreatedAt = Date.now();
            readyCount++;
            console.log(`[pool] Client ${entry.id} ready (${readyCount}/${this.poolSize})`);
          } catch (err) {
            console.error(`[pool] Client ${entry.id} warmup failed:`, err.message);
            entry.ready = false;
          } finally {
            entry.initializing = false;
          }
        })
      );

      // Stagger delay between batches (skip after last batch)
      if (batchEnd < this.entries.length) {
        await new Promise((resolve) => setTimeout(resolve, STAGGER_DELAY));
      }
    }

    if (readyCount === 0) {
      throw new Error("[pool] No clients could be initialized");
    }

    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
    console.log(`[pool] Warmup complete: ${readyCount}/${this.poolSize} clients ready in ${elapsed}s`);

    // Start background health check
    this.healthCheckTimer = setInterval(() => this._healthCheck(), HEALTH_CHECK_INTERVAL);
    this.healthCheckTimer.unref();
  }

  /**
   * Acquire a free (ready + not busy) client.
   * If none available, wait in queue with a timeout.
   * @param {number} [timeoutMs] Override queue timeout for this request
   * @returns {Promise<object>} Pool entry
   */
  async acquire(timeoutMs) {
    const timeout = timeoutMs || this.queueTimeout;

    // Try to find a free client immediately
    const free = this.entries.find((e) => e.ready && !e.busy);
    if (free) {
      free.busy = true;
      return free;
    }

    // No free client — wait in queue
    const readyCount = this.entries.filter((e) => e.ready).length;
    if (readyCount === 0) {
      throw new Error("[pool] No healthy clients available");
    }

    this._queuedCount++;
    // Only log every 10th queued request to reduce spam
    if (this._queuedCount % 10 === 1 || this._queuedCount <= 3) {
      console.log(`[pool] All ${readyCount} clients busy, queue depth: ${this._waitQueue.length + 1}`);
    }

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        // Remove from queue on timeout
        const idx = this._waitQueue.findIndex((w) => w.resolve === resolve);
        if (idx !== -1) this._waitQueue.splice(idx, 1);
        reject(new Error(`[pool] Queue timeout after ${timeout}ms — all clients busy`));
      }, timeout);

      this._waitQueue.push({ resolve, timer });
    });
  }

  /**
   * Release a client back to the pool. If requests are queued, hand it off.
   * @param {object} entry Pool entry returned by acquire()
   */
  release(entry) {
    if (this._waitQueue.length > 0 && entry.ready) {
      const next = this._waitQueue.shift();
      clearTimeout(next.timer);
      next.resolve(entry); // entry stays busy
    } else {
      entry.busy = false;
    }

    // Log a summary periodically instead of per-request
    this._completedCount++;
    if (this._completedCount % 10 === 0) {
      const busy = this.entries.filter((e) => e.busy).length;
      const queue = this._waitQueue.length;
      console.log(`[pool] Progress: ${this._completedCount} completed, ${busy} busy, ${queue} queued`);
    }
  }

  /**
   * Execute a function with an acquired client (1 request per client at a time).
   * @param {function} fn Async function receiving the MetaAIClient instance
   * @param {number} [timeoutMs] Optional queue timeout override
   * @returns {Promise<*>} Result of fn
   */
  async execute(fn, timeoutMs) {
    const entry = await this.acquire(timeoutMs);
    try {
      const result = await fn(entry.client);
      return result;
    } catch (err) {
      // If it looks like a session/auth error, mark client as unhealthy
      const msg = err.message || "";
      if (
        msg.includes("access token") ||
        msg.includes("LSD token") ||
        msg.includes("401") ||
        msg.includes("403") ||
        msg.includes("session") ||
        msg.includes("exhausted")
      ) {
        console.log(`[pool] Client ${entry.id} session error, marking unhealthy`);
        entry.ready = false;
        entry.sessionCreatedAt = null;
        entry.client.resetSession();
      }
      throw err;
    } finally {
      this.release(entry);
    }
  }

  /**
   * Background health check:
   * 1. Re-initialize any failed clients
   * 2. Proactively refresh idle clients with stale sessions (>10 min)
   */
  async _healthCheck() {
    // Re-init unhealthy clients
    const unhealthy = this.entries.filter(
      (e) => !e.ready && !e.initializing && !e.busy
    );

    if (unhealthy.length > 0) {
      console.log(`[pool] Health check: ${unhealthy.length} client(s) need re-init`);
      for (const entry of unhealthy) {
        try {
          entry.initializing = true;
          entry.client.resetSession();
          await entry.client.ensureSession();
          entry.ready = true;
          entry.sessionCreatedAt = Date.now();
          console.log(`[pool] Client ${entry.id} re-initialized`);
        } catch (err) {
          console.error(`[pool] Client ${entry.id} re-init failed:`, err.message);
        } finally {
          entry.initializing = false;
        }
      }
    }

    // Proactive token refresh: refresh idle clients with sessions older than 10 min
    const now = Date.now();
    const stale = this.entries.filter(
      (e) =>
        e.ready &&
        !e.busy &&
        !e.initializing &&
        e.sessionCreatedAt &&
        now - e.sessionCreatedAt > SESSION_MAX_AGE
    );

    if (stale.length > 0) {
      console.log(`[pool] Proactive refresh: ${stale.length} client(s) with stale sessions`);
      for (const entry of stale) {
        // Only refresh if still idle (another request may have grabbed it)
        if (entry.busy) continue;
        try {
          entry.initializing = true;
          entry.busy = true; // prevent acquire() from picking it up mid-refresh
          entry.client.resetSession();
          await entry.client.ensureSession();
          entry.sessionCreatedAt = Date.now();
          console.log(`[pool] Client ${entry.id} session refreshed`);
        } catch (err) {
          console.error(`[pool] Client ${entry.id} refresh failed:`, err.message);
          entry.ready = false;
          entry.sessionCreatedAt = null;
        } finally {
          entry.initializing = false;
          entry.busy = false;
        }
      }
    }
  }

  /**
   * Stop the health check timer and return pool stats.
   */
  shutdown() {
    if (this.healthCheckTimer) {
      clearInterval(this.healthCheckTimer);
      this.healthCheckTimer = null;
    }
    const ready = this.entries.filter((e) => e.ready).length;
    console.log(`[pool] Shutdown. ${ready}/${this.poolSize} clients were healthy. ${this._completedCount} requests served.`);
  }
}

module.exports = { ClientPool };
