# Meta AI Scraper

A CLI tool that queries Meta AI and returns structured JSON responses with text, sources, and markdown — no account required.

[![Demo](https://img.youtube.com/vi/IF_R-Cr29RA/maxresdefault.jpg)](https://youtu.be/IF_R-Cr29RA)

## How It Works

Meta AI was rebuilt as a Next.js app (as of March 2026) with a new `/api/graphql` endpoint using `doc_id`-based persisted queries and SSE streaming. The scraper operates in three stages per request:

1. **Challenge solving** — Visits `meta.ai` using `curl_cffi` with Chrome TLS fingerprinting. If a bot-detection challenge (`/__rd_verify_`) is returned, solves it to set required session cookies (`datr`, `rd_challenge`).

2. **Access token** — Calls the `acceptTOSForLoggedOut` GraphQL mutation to accept the Terms of Service as an anonymous user, returning a temporary Bearer token and `abraUserId`.

3. **Message + Sources** — Sends the user's prompt via the `sendMessageStream` SSE endpoint with the Bearer token. The streaming response is parsed for the final content and any inline sources (no separate fetch needed).

## Project Structure

```
python/
  main.py            CLI entry point (single query)
  runner.py          Batch runner with SQLite storage
  meta_client.py     Core scraper: challenge solving, token auth, SSE message sending
  constants.py       Named constants (timeouts, retry limits, batch defaults)
  exceptions.py      Custom exception hierarchy
  check_results.py   Terminal viewer + CSV export
  dashboard.py       Streamlit web dashboard
  requirements.txt   Python dependencies
data/
  meta-ai.db         SQLite database (created by runner.py)
  results.csv        CSV export (created by check_results.py --csv)
```

## Prerequisites

- **Python** >= 3.10
- A **proxy provider** (optional but recommended for scale — Meta AI blocks many datacenter IPs)

## Setup

```bash
pip install -r python/requirements.txt
```

## Usage

### Single query

```bash
# Windows
python python/main.py "What are the latest developments in AI?"

# Linux / macOS
python3 python/main.py "What are the latest developments in AI?"
```

With a proxy:

```bash
python python/main.py "What is quantum computing?" --proxy http://user:pass@host:port
```

### Batch run (at scale)

Create a `.env` file in the project root with your configuration:

```
PROXY_URL=http://user:pass@host:port
PROMPT=What do you know about Tesla's latest updates?
TOTAL_REQUESTS=1000
PARALLEL_REQUESTS=500
MAX_RETRIES=2
```

Then run:

```bash
# Windows
python python/runner.py

# Linux / macOS
python3 python/runner.py
```

Results are saved to `data/meta-ai.db`.

## Output

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
    "html": "",
    "markdown": "**Markdown** formatted response...",
    "searchQueries": [],
    "shoppingCards": [],
    "model": "meta-ai"
  }
}
```

## Viewing Results

After a batch run, there are three ways to explore the results stored in `data/meta-ai.db`.

### Terminal summary

```bash
# Windows
python python/check_results.py            # one-line summary per request
python python/check_results.py -d         # include full response JSON
python python/check_results.py -d 5       # full JSON for request #5 only

# Linux / macOS
python3 python/check_results.py
python3 python/check_results.py -d
python3 python/check_results.py -d 5
```

### CSV export

Export all results to `data/results.csv` for use in Excel or Google Sheets:

```bash
# Windows
python python/check_results.py --csv

# Linux / macOS
python3 python/check_results.py --csv
```

Columns: `id`, `timestamp`, `success`, `duration_ms`, `text_length`, `source_count`, `model`, `error_or_preview`.

### Web dashboard (Streamlit)

Interactive browser dashboard with summary cards, charts, and a searchable results table.

```bash
# Windows
streamlit run python/dashboard.py

# Linux / macOS
streamlit run python/dashboard.py
```

Opens at [http://localhost:8501](http://localhost:8501). Features:

- **Summary cards** — total requests, successes, failures, success rate, avg duration
- **Charts** — success/fail breakdown, duration histogram, rolling success rate
- **Results table** — filterable by status, searchable by response content
- **Expandable JSON** — click any row to inspect the full response

### DB Browser for SQLite

For ad-hoc SQL queries, open `data/meta-ai.db` directly with [DB Browser for SQLite](https://sqlitebrowser.org/dl/) (free, cross-platform).

## Design Decisions

### TLS Fingerprinting

`curl_cffi` impersonates Chrome's TLS fingerprint, bypassing Meta AI's bot detection without needing a headless browser.

### No System Prompt Injection

The user's prompt is sent directly to Meta AI. The raw response is structured server-side because:

- **Real sources** — Meta AI returns sources inline in the SSE stream via `contentRenderer`. Injecting a system prompt causes the model to fabricate URLs instead of returning real ones.
- **Reliability** — Meta AI doesn't consistently follow structured output instructions. Parsing the SSE stream is more reliable.

### Session Management

If a session is exhausted (token expired or rate-limited), the client resets the Bearer token and `abraUserId`, re-runs the challenge + TOS flow, then retries once.

### Response Quality Validation

The batch runner validates every response before marking it as a success. Empty responses and short cop-out answers (< 200 chars with no sources) trigger automatic retries with a fresh proxy IP, up to `MAX_RETRIES` times.
