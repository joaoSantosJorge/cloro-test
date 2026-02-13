# Meta AI Scraper

A high-throughput scraper for Meta AI that extracts responses, sources, and structured data without requiring a signed-in account. Designed to run locally and handle 1000+ concurrent requests with a 95%+ success rate.

## How It Works

The scraper operates in three stages per request:

1. **Cookie extraction** -- A headless Chrome instance (via Puppeteer Stealth) visits `meta.ai` to extract session tokens (`LSD`, `datr`, `abra_csrf`). Meta AI uses TLS fingerprinting and bot detection that blocks plain HTTP clients, so a real browser is required for this step.

2. **Access token** -- Using the extracted cookies, the scraper calls Meta's GraphQL API to accept the Terms of Service as a temporary (anonymous) user, which returns a short-lived OAuth access token.

3. **Message + Sources** -- The access token is used to send the user's prompt via Meta's `sendMessage` GraphQL mutation. The streamed NDJSON response is parsed to extract the full text. If Meta AI references web sources, a follow-up `fetchSources` query resolves the real URLs.

The server exposes a single REST endpoint that accepts a prompt and returns a structured JSON response with text, markdown, HTML, sources, and search queries.

## Architecture & Design Decisions

### Client Pool

Instead of a single client, the server maintains a **pool of N independent MetaAIClient instances** (default 15). Each client has its own cookies, access token, and user agent. This provides:

- **Concurrency** -- Multiple requests are served in parallel, each by a different client.
- **Fault isolation** -- If one client's session expires or gets rate-limited, others continue working. Failed clients are automatically re-initialized by a background health check.
- **Session rotation** -- Tokens are proactively refreshed every 10 minutes before they go stale.

Requests that arrive when all clients are busy wait in a FIFO queue with a configurable timeout.

### Browser Semaphore

Chrome instances are expensive (~200-400MB RAM each). A semaphore limits concurrent browser launches to 3 (configurable via `MAX_CONCURRENT_BROWSERS` in `meta-client.js`), preventing OOM crashes during warmup or mass session refreshes.

### Proxy Support

All HTTP requests (both browser and API) can be routed through an HTTP proxy for IP rotation. The proxy URL is passed to:

- **Puppeteer** as `--proxy-server` with separate `page.authenticate()` for credentials.
- **fetch() calls** via `undici`'s `ProxyAgent` as the `dispatcher` option.

This lets you use residential or datacenter proxy providers (e.g., IPRoyal) to avoid IP-based rate limiting at scale.

### Stealth & Anti-Detection

- **puppeteer-extra-plugin-stealth** patches common Puppeteer detection vectors (webdriver flag, chrome.runtime, etc.).
- Each pool client is assigned a **distinct User-Agent** from a rotating pool of 16 realistic browser strings (Chrome, Firefox, Edge, Safari across Windows/Mac/Linux).
- Resource blocking (images, fonts, stylesheets) reduces bandwidth and speeds up cookie extraction.

### Response Parsing

Meta AI streams responses as NDJSON (newline-delimited JSON). The parser:

1. Iterates every line, extracting `bot_response_message` from streamed updates.
2. Keeps the **last valid response** (which contains the full accumulated text).
3. Extracts inline source URLs (`l.meta.ai` redirects) and resolves them via a dedicated `fetchSources` GraphQL query when a `fetch_id` is available.
4. Builds the final structured response server-side (plain text, markdown, HTML via `marked`).

### Persistence

Every response (success or failure) is logged to a local **SQLite database** (`data/meta-ai.db`) using `better-sqlite3` with WAL mode for concurrent read performance. This enables post-run analysis of success rates, latencies, and failure modes.

## Project Structure

```
server.js           Express server, single POST endpoint, retry logic
client-pool.js      Pool manager: warmup, acquire/release, health checks
meta-client.js      Core scraper: cookie extraction, token auth, message sending
database.js         SQLite persistence layer
test-parallel.js    Load test script with configurable concurrency
.env.example        Environment variable template
```

## Prerequisites

- **Node.js** >= 18 (uses native `fetch`)
- **Google Chrome** installed locally (Puppeteer connects to it via `puppeteer-core`)
- A **proxy provider** (optional but recommended for scale)

## Setup

1. **Clone the repository**

   ```bash
   git clone <repo-url>
   cd cloro-try-1
   ```

2. **Install dependencies**

   ```bash
   npm install
   ```

3. **Configure environment**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` with your settings:

   ```dotenv
   PROXY_URL=http://user:password@proxy-host:port   # leave empty for direct
   POOL_SIZE=15                                       # number of parallel clients
   QUEUE_TIMEOUT=300000                                # max wait in queue (ms)
   CONCURRENCY=200                                     # for test script
   MAX_IN_FLIGHT=30                                    # for test script
   ```

4. **Set Chrome path** (if not in default location)

   The default is `C:/Program Files/Google/Chrome/Application/chrome.exe`. Override with:

   ```dotenv
   CHROME_PATH=/usr/bin/google-chrome
   ```

## Running

**Start the server:**

```bash
npm start
```

The server warms up all pool clients (extracting cookies + tokens in batches), then listens on port 8000.

**Send a request:**

```bash
curl -X POST http://localhost:8000/v1/monitor/meta-ai \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What are the latest developments in AI?", "country": "US", "include": {"markdown": true}}'
```

**Run the load test:**

```bash
npm run test:parallel
```

Or with custom settings:

```bash
CONCURRENCY=50 MAX_IN_FLIGHT=10 npm run test:parallel
```

## API

### `POST /v1/monitor/meta-ai`

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | yes | The question to ask Meta AI |
| `country` | string | no | Country code (default: `"US"`) |
| `include.markdown` | boolean | no | Include `html` and `markdown` fields in response |

**Response:**

```json
{
  "success": true,
  "result": {
    "text": "Plain text response...",
    "sources": [
      {
        "position": 0,
        "url": "https://example.com",
        "label": "Example Source",
        "description": "A brief description"
      }
    ],
    "html": "<div class=\"markdown\">...</div>",
    "markdown": "**Markdown** formatted response...",
    "searchQueries": [],
    "shoppingCards": [],
    "model": "meta-ai"
  }
}
```

### `GET /health`

Returns `{"status": "ok"}`.
