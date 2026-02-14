#!/usr/bin/env bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${SCRIPT_DIR}/bin"

# --- Configuration ---
# curl-impersonate release from https://github.com/lwthiker/curl-impersonate
VERSION="${CURL_IMPERSONATE_VERSION:-0.5.4}"
ARCH="x86_64-linux-gnu"
TARBALL="curl-impersonate-v${VERSION}.${ARCH}.tar.gz"
URL="https://github.com/lwthiker/curl-impersonate/releases/download/v${VERSION}/${TARBALL}"

# The Chrome wrapper we'll use (matches Chrome 110 TLS fingerprint)
CHROME_WRAPPER="curl_chrome110"

# --- Check if already installed ---
if [[ -x "${BIN_DIR}/${CHROME_WRAPPER}" ]]; then
  echo "curl-impersonate already installed at ${BIN_DIR}/${CHROME_WRAPPER}"
  exit 0
fi

mkdir -p "$BIN_DIR"

# --- Download ---
echo "Downloading curl-impersonate v${VERSION}..."
echo "  URL: ${URL}"

if ! curl -fL --progress-bar -o "/tmp/${TARBALL}" "$URL"; then
  echo ""
  echo "Download failed. Possible fixes:"
  echo "  1. Check your internet connection"
  echo "  2. Try a different version: CURL_IMPERSONATE_VERSION=0.6.1 $0"
  echo "  3. Download manually from: https://github.com/lwthiker/curl-impersonate/releases"
  echo "     Extract to: ${BIN_DIR}/"
  exit 1
fi

# --- Extract ---
echo "Extracting to ${BIN_DIR}..."
tar xzf "/tmp/${TARBALL}" -C "$BIN_DIR"
rm -f "/tmp/${TARBALL}"

# Make binaries executable
chmod +x "${BIN_DIR}/${CHROME_WRAPPER}" "${BIN_DIR}/curl-impersonate-chrome" 2>/dev/null || true

# --- Verify ---
echo ""
if "${BIN_DIR}/${CHROME_WRAPPER}" --version 2>/dev/null; then
  echo ""
  echo "SUCCESS: curl-impersonate installed!"
  echo "  Binary: ${BIN_DIR}/${CHROME_WRAPPER}"
else
  echo "WARNING: Binary downloaded but --version check failed."
  echo "  This may still work. Try: ${BIN_DIR}/${CHROME_WRAPPER} https://httpbin.org/headers"
fi
