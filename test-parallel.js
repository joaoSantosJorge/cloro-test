/**
 * Parallel test script — fires N concurrent POST requests to the Meta AI scraper.
 *
 * Environment variables:
 *   CONCURRENCY   — number of parallel requests (default: 100)
 *   TEST_PROMPT   — the prompt to send (default: a generic test prompt)
 *   PORT          — server port (default: 8000)
 *   API_KEY       — optional Bearer token
 *
 * NOTE: Do NOT use "PROMPT" as the env var name — on Windows it's a
 *       built-in variable (default "$P$G") that controls the cmd.exe prompt.
 */

require("dotenv").config({ override: true });

const CONCURRENCY = parseInt(process.env.CONCURRENCY, 10) || 100;
const MAX_IN_FLIGHT = parseInt(process.env.MAX_IN_FLIGHT, 10) || 50;
const TEST_PROMPT = process.env.TEST_PROMPT || "What are the latest developments in AI?";
const PORT = process.env.PORT || 8000;
const API_KEY = process.env.API_KEY || "sk_live_test";
const BASE_URL = `http://localhost:${PORT}`;

// Shared counter for live progress
let completed = 0;

async function sendRequest(index) {
  const start = Date.now();
  try {
    const resp = await fetch(`${BASE_URL}/v1/monitor/meta-ai`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${API_KEY}`,
      },
      body: JSON.stringify({
        prompt: `${TEST_PROMPT} (request #${index + 1})`,
        country: "US",
        include: { markdown: true },
      }),
    });

    const data = await resp.json();
    const durationMs = Date.now() - start;
    const textLen = data.result?.text?.length || 0;
    const sourceCount = data.result?.sources?.length || 0;

    completed++;
    const status = data.success ? "OK" : "FAIL";
    console.log(
      `[${String(completed).padStart(String(CONCURRENCY).length)}/${CONCURRENCY}] ` +
      `#${String(index + 1).padStart(String(CONCURRENCY).length)} ${status} ${durationMs}ms` +
      (data.success ? ` text=${textLen} sources=${sourceCount}` : ` error="${data.error}"`)
    );

    return {
      index: index + 1,
      status: resp.status,
      success: data.success,
      durationMs,
      textLen,
      sourceCount,
      error: data.error || null,
    };
  } catch (err) {
    const durationMs = Date.now() - start;
    completed++;
    console.log(
      `[${String(completed).padStart(String(CONCURRENCY).length)}/${CONCURRENCY}] ` +
      `#${String(index + 1).padStart(String(CONCURRENCY).length)} ERR ${durationMs}ms error="${err.message}"`
    );
    return {
      index: index + 1,
      status: 0,
      success: false,
      durationMs,
      textLen: 0,
      sourceCount: 0,
      error: err.message,
    };
  }
}

/**
 * Calculate a percentile from a sorted array of numbers.
 */
function percentile(sorted, p) {
  const idx = Math.ceil((p / 100) * sorted.length) - 1;
  return sorted[Math.max(0, idx)];
}

async function main() {
  console.log(`\n=== Parallel Test ===`);
  console.log(`Total requests: ${CONCURRENCY}`);
  console.log(`Max in-flight: ${MAX_IN_FLIGHT}`);
  console.log(`Prompt: "${TEST_PROMPT}"`);
  console.log(`Server: ${BASE_URL}`);
  console.log(`\nFiring ${CONCURRENCY} requests (max ${MAX_IN_FLIGHT} in-flight)...\n`);

  const overallStart = Date.now();

  // Throttle: keep at most MAX_IN_FLIGHT requests running at once
  const results = [];
  let nextIndex = 0;

  async function runNext() {
    while (nextIndex < CONCURRENCY) {
      const i = nextIndex++;
      const result = await sendRequest(i);
      results.push(result);
    }
  }

  const workers = Array.from({ length: Math.min(MAX_IN_FLIGHT, CONCURRENCY) }, () => runNext());
  await Promise.all(workers);

  const overallDuration = Date.now() - overallStart;

  // Compute latency percentiles
  const durations = results.map((r) => r.durationMs).sort((a, b) => a - b);
  const p50 = percentile(durations, 50);
  const p95 = percentile(durations, 95);
  const p99 = percentile(durations, 99);

  // Summary
  const succeeded = results.filter((r) => r.success).length;
  const failed = results.filter((r) => !r.success).length;
  const avgDuration = Math.round(
    results.reduce((sum, r) => sum + r.durationMs, 0) / results.length
  );
  const maxDuration = Math.max(...durations);
  const minDuration = Math.min(...durations);

  console.log(`\n--- Summary ---`);
  console.log(`  Total:     ${CONCURRENCY} requests`);
  console.log(`  Succeeded: ${succeeded} (${((succeeded / CONCURRENCY) * 100).toFixed(1)}%)`);
  console.log(`  Failed:    ${failed}`);
  console.log(`  Wall time: ${overallDuration}ms (${(overallDuration / 1000).toFixed(1)}s)`);
  console.log(`  Avg:       ${avgDuration}ms`);
  console.log(`  Min:       ${minDuration}ms`);
  console.log(`  p50:       ${p50}ms`);
  console.log(`  p95:       ${p95}ms`);
  console.log(`  p99:       ${p99}ms`);
  console.log(`  Max:       ${maxDuration}ms`);
  console.log();

  process.exit(failed > 0 ? 1 : 0);
}

main();
