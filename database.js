const path = require("path");
const fs = require("fs");

const DB_PATH = path.join(__dirname, "data", "meta-ai.db");

let db = null;

/**
 * Initialize the SQLite database. Creates the data/ directory and
 * responses table if they don't exist. Returns the db instance.
 */
function initDb() {
  const Database = require("better-sqlite3");

  fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });

  db = new Database(DB_PATH);
  db.pragma("journal_mode = WAL"); // better concurrent read performance

  db.exec(`
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
  `);

  console.log("[db] SQLite database ready at", DB_PATH);
  return db;
}

/**
 * Save a response row.
 * @param {object} meta - { id, timestamp, durationMs, retried, prompt, country, statusCode }
 * @param {object} result - The full API result payload
 */
function saveResponse(meta, result) {
  if (!db) throw new Error("Database not initialized — call initDb() first");

  const stmt = db.prepare(`
    INSERT INTO responses (
      id, timestamp, timestamp_unix, duration_ms, retried,
      prompt, country, status_code, success,
      text_length, source_count, model, result_json
    ) VALUES (
      @id, @timestamp, @timestamp_unix, @duration_ms, @retried,
      @prompt, @country, @status_code, @success,
      @text_length, @source_count, @model, @result_json
    )
  `);

  stmt.run({
    id: meta.id,
    timestamp: meta.timestamp,
    timestamp_unix: meta.timestampUnix,
    duration_ms: meta.durationMs,
    retried: meta.retried ? 1 : 0,
    prompt: meta.prompt || "",
    country: meta.country || "US",
    status_code: meta.statusCode,
    success: result.success ? 1 : 0,
    text_length: result.result?.text?.length || 0,
    source_count: result.result?.sources?.length || 0,
    model: result.result?.model || null,
    result_json: JSON.stringify(result),
  });
}

/**
 * Fetch a response by id.
 * @param {string} id
 * @returns {object|undefined}
 */
function getResponse(id) {
  if (!db) throw new Error("Database not initialized — call initDb() first");
  const row = db.prepare("SELECT * FROM responses WHERE id = ?").get(id);
  if (row && row.result_json) {
    row.result = JSON.parse(row.result_json);
  }
  return row;
}

module.exports = { initDb, saveResponse, getResponse };
