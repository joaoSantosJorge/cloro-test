#!/usr/bin/env bash
#
# meta-prompt.sh — Send a single prompt to Meta AI via curl-impersonate.
#
# Usage:
#   ./meta-prompt.sh "What is the capital of France?"
#   ./meta-prompt.sh "Tell me about AI" --proxy http://user:pass@host:port
#
# Dependencies: curl-impersonate (run install-curl-impersonate.sh), jq, python3
#
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CURL_CMD="${SCRIPT_DIR}/bin/curl_chrome110"

# ─── Dependency checks ───────────────────────────────────────────────
if [[ ! -x "$CURL_CMD" ]]; then
  echo "ERROR: curl-impersonate not found at ${CURL_CMD}"
  echo "  Run: ./install-curl-impersonate.sh"
  exit 1
fi
for dep in jq python3; do
  if ! command -v "$dep" &>/dev/null; then
    echo "ERROR: ${dep} is required but not found."
    exit 1
  fi
done

# ─── Parse arguments ─────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 \"your prompt\" [--proxy http://user:pass@host:port]"
  exit 1
fi

PROMPT="$1"
shift

PROXY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --proxy) PROXY="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ─── Helper: curl with optional proxy ────────────────────────────────
do_curl() {
  if [[ -n "$PROXY" ]]; then
    "$CURL_CMD" --proxy "$PROXY" "$@"
  else
    "$CURL_CMD" "$@"
  fi
}

# ─── Temp files (cleaned up on exit) ─────────────────────────────────
HTML_FILE=$(mktemp)
RESPONSE_FILE=$(mktemp)
trap 'rm -f "$HTML_FILE" "$RESPONSE_FILE"' EXIT

# ─── User-Agent matching Chrome 110 (must match curl_chrome110 TLS fingerprint) ──
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"

# =====================================================================
# STEP 1: Fetch cookies from Meta AI homepage
# =====================================================================
echo "[1/3] Fetching cookies from meta.ai..."

HTTP_CODE=$(do_curl -s -L -w '%{http_code}' \
  -H "User-Agent: ${UA}" \
  -H "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8" \
  -H "Accept-Language: en-US,en;q=0.9" \
  -H "Cache-Control: no-cache" \
  -H "Sec-Fetch-Dest: document" \
  -H "Sec-Fetch-Mode: navigate" \
  -H "Sec-Fetch-Site: none" \
  -H "Sec-Fetch-User: ?1" \
  -H "Upgrade-Insecure-Requests: 1" \
  -o "$HTML_FILE" \
  "https://www.meta.ai/")

echo "  HTTP status: ${HTTP_CODE}"
echo "  HTML size:   $(wc -c < "$HTML_FILE") bytes"

if [[ "$HTTP_CODE" != "200" ]]; then
  echo "ERROR: Got HTTP ${HTTP_CODE} from meta.ai"
  head -c 500 "$HTML_FILE" 2>/dev/null || true
  exit 1
fi

# ─── Extract tokens from server-rendered HTML ────────────────────────
# These mirror the exact patterns from meta-client.js extractValue()
LSD=$(grep -o '"LSD",\[\],{"token":"[^"]*"' "$HTML_FILE" | head -1 | sed 's/.*"token":"//;s/".*//')
DATR=$(grep -o '"datr":{"value":"[^"]*"' "$HTML_FILE" | head -1 | sed 's/.*"value":"//;s/".*//')
JS_DATR=$(grep -o '"_js_datr":{"value":"[^"]*"' "$HTML_FILE" | head -1 | sed 's/.*"value":"//;s/".*//')
ABRA_CSRF=$(grep -o '"abra_csrf":{"value":"[^"]*"' "$HTML_FILE" | head -1 | sed 's/.*"value":"//;s/".*//')
FB_DTSG=$(grep -o '"DTSGInitData",\[\],{"token":"[^"]*"' "$HTML_FILE" | head -1 | sed 's/.*"token":"//;s/".*//')

if [[ -z "$LSD" ]]; then
  echo "ERROR: Failed to extract LSD token."
  echo "  Bot detection likely triggered. Try with --proxy."
  echo "  HTML preview:"
  head -c 500 "$HTML_FILE"
  exit 1
fi

echo "  lsd:       ${LSD:0:12}..."
echo "  datr:      ${DATR:0:12}..."
echo "  _js_datr:  ${JS_DATR:0:12}..."
echo "  abra_csrf: ${ABRA_CSRF:0:12}..."
[[ -n "$FB_DTSG" ]] && echo "  fb_dtsg:   ${FB_DTSG:0:12}..."

# Build cookie header
COOKIE="_js_datr=${JS_DATR}; datr=${DATR}; abra_csrf=${ABRA_CSRF}; ps_n=1; ps_l=1; dpr=2"

# =====================================================================
# STEP 2: Accept TOS → get anonymous access token
# =====================================================================
echo ""
echo "[2/3] Getting access token..."

TOKEN_RESPONSE=$(do_curl -s \
  -X POST \
  -H "User-Agent: ${UA}" \
  -H "Accept: */*" \
  -H "Accept-Language: en-US,en;q=0.9" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Cookie: ${COOKIE}" \
  -H "Origin: https://www.meta.ai" \
  -H "Referer: https://www.meta.ai/" \
  -H "Sec-Fetch-Dest: empty" \
  -H "Sec-Fetch-Mode: cors" \
  -H "Sec-Fetch-Site: same-origin" \
  -H "X-Fb-Friendly-Name: useAbraAcceptTOSForTempUserMutation" \
  -H "X-Fb-Lsd: ${LSD}" \
  -H "X-Asbd-Id: 129477" \
  --data-urlencode "lsd=${LSD}" \
  --data-urlencode "fb_dtsg=${FB_DTSG}" \
  --data-urlencode "fb_api_caller_class=RelayModern" \
  --data-urlencode "fb_api_req_friendly_name=useAbraAcceptTOSForTempUserMutation" \
  --data-urlencode 'variables={"dob":"1999-01-01","tos_accepted":true}' \
  --data-urlencode "doc_id=7604648749596940" \
  "https://www.meta.ai/api/graphql/")

# Response is NDJSON — find the line with the access token
ACCESS_TOKEN=""
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  token=$(echo "$line" | jq -r '.data.xab_abra_accept_terms_of_service.new_temp_user_auth.access_token // empty' 2>/dev/null)
  if [[ -n "$token" ]]; then
    ACCESS_TOKEN="$token"
    break
  fi
done <<< "$TOKEN_RESPONSE"

if [[ -z "$ACCESS_TOKEN" ]]; then
  echo "ERROR: Failed to extract access token."
  echo "  Response preview:"
  echo "${TOKEN_RESPONSE:0:500}"
  exit 1
fi

echo "  Token: ${ACCESS_TOKEN:0:20}..."

# =====================================================================
# STEP 3: Send the prompt
# =====================================================================
echo ""
echo "[3/3] Sending prompt: \"${PROMPT:0:60}\"..."

# Generate IDs matching Meta's format
CONV_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
THREADING_ID=$(python3 -c "
import time, os
ts = int(time.time() * 1000)
rand = int.from_bytes(os.urandom(8), 'big')
mask22 = (1 << 22) - 1
max64 = (1 << 64) - 1
print(((ts << 22) | (rand & mask22)) & max64)
")

# Build variables JSON (jq handles escaping safely)
VARIABLES=$(jq -n --compact-output \
  --arg prompt "$PROMPT" \
  --arg convId "$CONV_ID" \
  --arg threadId "$THREADING_ID" \
  '{
    message: {sensitive_string_value: $prompt},
    externalConversationId: $convId,
    offlineThreadingId: $threadId,
    suggestedPromptIndex: null,
    flashVideoRecapInput: {images: []},
    flashPreviewInput: null,
    promptPrefix: null,
    entrypoint: "ABRA__CHAT__TEXT",
    icebreaker_type: "TEXT"
  }')

do_curl -s \
  -X POST \
  -H "User-Agent: ${UA}" \
  -H "Accept: */*" \
  -H "Accept-Language: en-US,en;q=0.9" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Origin: https://www.meta.ai" \
  -H "Referer: https://www.meta.ai/" \
  -H "Sec-Fetch-Dest: empty" \
  -H "Sec-Fetch-Mode: cors" \
  -H "Sec-Fetch-Site: same-site" \
  -H "Authorization: OAuth ${ACCESS_TOKEN}" \
  --data-urlencode "fb_api_caller_class=RelayModern" \
  --data-urlencode "fb_api_req_friendly_name=useAbraSendMessageMutation" \
  --data-urlencode "variables=${VARIABLES}" \
  --data-urlencode "server_timestamps=true" \
  --data-urlencode "doc_id=7783822248314888" \
  -o "$RESPONSE_FILE" \
  "https://graph.meta.ai/graphql?locale=user"

# =====================================================================
# Parse the NDJSON streaming response
# =====================================================================
RESPONSE_SIZE=$(wc -c < "$RESPONSE_FILE")
RESPONSE_LINES=$(grep -c '' "$RESPONSE_FILE" 2>/dev/null || echo 0)
echo "  Response: ${RESPONSE_SIZE} bytes, ${RESPONSE_LINES} lines"

# Check for exhausted/invalid session
if [[ "$RESPONSE_SIZE" -lt 1000 ]]; then
  if grep -q 'missing_required_variable_value\|"bot_response_message":null' "$RESPONSE_FILE" 2>/dev/null; then
    echo "ERROR: Session exhausted or invalid token."
    cat "$RESPONSE_FILE"
    exit 1
  fi
fi

# Extract the last complete bot response (streaming sends incremental updates,
# the last one with content is the complete response)
BOT_TEXT=$(grep -v '^$' "$RESPONSE_FILE" | jq -r '
  (
    .data.node.bot_response_message //
    .data.xfb_abra_send_message.bot_response_message //
    empty
  ) |
  select(.composed_text.content | length > 0) |
  [.composed_text.content[].text] | join("")
' 2>/dev/null | tail -1)

if [[ -z "$BOT_TEXT" ]]; then
  echo ""
  echo "WARNING: Could not extract text from response."
  echo "Raw response preview:"
  head -c 1000 "$RESPONSE_FILE"
  exit 1
fi

echo ""
echo "========== META AI RESPONSE =========="
echo "$BOT_TEXT"
echo "======================================"
