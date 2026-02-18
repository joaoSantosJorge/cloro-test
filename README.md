# Meta AI Scraper

A CLI tool that queries Meta AI and returns structured JSON responses with text, sources, and markdown — no account required.

[![Demo](https://img.youtube.com/vi/IF_R-Cr29RA/maxresdefault.jpg)](https://youtu.be/IF_R-Cr29RA)

## How It Works

The scraper operates in three stages per request:

1. **Cookie extraction** — Visits `meta.ai` using `curl_cffi` with Chrome TLS fingerprinting to extract session tokens (`LSD`, `datr`, `abra_csrf`).

2. **Access token** — Using the extracted cookies, calls Meta's GraphQL API to accept the Terms of Service as a temporary (anonymous) user, returning a short-lived OAuth access token.

3. **Message + Sources** — The access token sends the user's prompt via Meta's `sendMessage` GraphQL mutation. The streamed NDJSON response is parsed to extract the full text. If Meta AI references web sources, a follow-up `fetchSources` query resolves the real URLs.

## Project Structure

```
python/
  main.py            CLI entry point (single query)
  runner.py          Batch runner with SQLite storage
  meta_client.py     Core scraper: cookie extraction, token auth, message sending
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
PARALLEL_REQUESTS=50
MAX_RETRIES=2
```

> **Windows limit:** `PARALLEL_REQUESTS` should stay at **50–100** on Windows. The `select()` syscall has a 512 file descriptor cap, and each request opens multiple sockets. Values above ~150 will crash.

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

- **Real sources** — Meta AI naturally includes `l.meta.ai` redirect URLs and `fetch_id` references that resolve to actual web sources. Injecting a system prompt causes the model to fabricate URLs.
- **Reliability** — Meta AI doesn't consistently follow structured output instructions. Parsing the NDJSON stream is more reliable.

### Session Management

If a session is exhausted (token expired or rate-limited), the client automatically refreshes cookies and token, then retries once.

### Response Quality Validation

The batch runner validates every response before marking it as a success. Empty responses and short cop-out answers (< 200 chars with no sources) trigger automatic retries with a fresh proxy IP, up to `MAX_RETRIES` times.
