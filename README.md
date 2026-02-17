# Meta AI Scraper

A CLI tool that queries Meta AI and returns structured JSON responses with text, sources, and markdown — no account required.

## How It Works

The scraper operates in three stages per request:

1. **Cookie extraction** — Visits `meta.ai` using `curl_cffi` with Chrome TLS fingerprinting to extract session tokens (`LSD`, `datr`, `abra_csrf`).

2. **Access token** — Using the extracted cookies, calls Meta's GraphQL API to accept the Terms of Service as a temporary (anonymous) user, returning a short-lived OAuth access token.

3. **Message + Sources** — The access token sends the user's prompt via Meta's `sendMessage` GraphQL mutation. The streamed NDJSON response is parsed to extract the full text. If Meta AI references web sources, a follow-up `fetchSources` query resolves the real URLs.

## Project Structure

```
python/
  main.py            CLI entry point
  meta_client.py     Core scraper: cookie extraction, token auth, message sending
  requirements.txt   Python dependencies
```

## Prerequisites

- **Python** >= 3.10
- A **proxy provider** (optional but recommended for scale — Meta AI blocks many datacenter IPs)

## Setup

```bash
pip install -r python/requirements.txt
```

## Usage

```bash
python python/main.py "What are the latest developments in AI?"
```

With a proxy:

```bash
python python/main.py "What is quantum computing?" --proxy http://user:pass@host:port
```

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

## Design Decisions

### TLS Fingerprinting

`curl_cffi` impersonates Chrome's TLS fingerprint, bypassing Meta AI's bot detection without needing a headless browser.

### No System Prompt Injection

The user's prompt is sent directly to Meta AI. The raw response is structured server-side because:

- **Real sources** — Meta AI naturally includes `l.meta.ai` redirect URLs and `fetch_id` references that resolve to actual web sources. Injecting a system prompt causes the model to fabricate URLs.
- **Reliability** — Meta AI doesn't consistently follow structured output instructions. Parsing the NDJSON stream is more reliable.

### Session Management

If a session is exhausted (token expired or rate-limited), the client automatically refreshes cookies and token, then retries once.
