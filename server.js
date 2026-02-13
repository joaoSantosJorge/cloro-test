require("dotenv").config({ override: true });
const express = require("express");
const crypto = require("crypto");
const { ClientPool } = require("./client-pool");
const { initDb, saveResponse } = require("./database");

const app = express();
app.use(express.json());

const POOL_SIZE = parseInt(process.env.POOL_SIZE, 10) || 15;
const QUEUE_TIMEOUT = parseInt(process.env.QUEUE_TIMEOUT, 10) || 120_000;
const PROXY_URL = process.env.PROXY_URL || null;
const pool = new ClientPool(POOL_SIZE, { queueTimeout: QUEUE_TIMEOUT, proxy: PROXY_URL });

app.post("/v1/monitor/meta-ai", async (req, res) => {
  const startTime = Date.now();
  let retried = false;

  try {
    const { prompt, country = "US", include } = req.body;

    if (!prompt) {
      return res.status(400).json({ success: false, error: "prompt is required" });
    }

    console.log(`[server] Received prompt: "${prompt.substring(0, 50)}..." (country=${country})`);

    let result;
    try {
      result = await pool.execute((client) => client.prompt(prompt));
    } catch (err) {
      // Retry once via pool (may pick a different healthy client)
      console.error("[server] First attempt failed:", err.message);
      retried = true;
      result = await pool.execute((client) => client.prompt(prompt));
    }

    // If markdown not requested, strip html/markdown fields
    if (!include?.markdown) {
      delete result.result.html;
      delete result.result.markdown;
    }

    const now = new Date();
    const id = crypto.randomBytes(4).toString("hex");
    saveResponse(
      {
        id,
        timestamp: now.toISOString(),
        timestampUnix: now.getTime(),
        durationMs: Date.now() - startTime,
        retried,
        prompt: req.body.prompt,
        country: req.body.country || "US",
        statusCode: 200,
      },
      result
    );

    return res.json(result);
  } catch (err) {
    console.error("[server] Error:", err.message);
    const errorResult = { success: false, error: err.message };

    const now = new Date();
    const id = crypto.randomBytes(4).toString("hex");
    saveResponse(
      {
        id,
        timestamp: now.toISOString(),
        timestampUnix: now.getTime(),
        durationMs: Date.now() - startTime,
        retried,
        prompt: req.body?.prompt || "",
        country: req.body?.country || "US",
        statusCode: 502,
      },
      errorResult
    );

    return res.status(502).json(errorResult);
  }
});

app.get("/health", (_req, res) => {
  res.json({ status: "ok" });
});

const PORT = process.env.PORT || 8000;

// Initialize database, then warm up the pool, then start listening
initDb();
pool.warmup().then(() => {
  app.listen(PORT, () => {
    console.log(`[server] Meta AI scraper running on http://localhost:${PORT}`);
    console.log(`[server] Pool size: ${POOL_SIZE}, Queue timeout: ${QUEUE_TIMEOUT}ms`);
    console.log(`[server] Proxy: ${PROXY_URL ? PROXY_URL.replace(/\/\/(.+?)@/, "//*****@") : "none (direct)"}`);

  });
}).catch((err) => {
  console.error("[server] Failed to start:", err.message);
  process.exit(1);
});
