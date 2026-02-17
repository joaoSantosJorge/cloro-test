"""
Core Meta AI client — cookie extraction, token auth, and message sending.

Uses curl_cffi for TLS-fingerprinted HTTP requests (impersonates Chrome),
eliminating the need for a headless browser for most operations.
Falls back to Playwright for cookie extraction when curl_cffi gets blocked.
"""

import json
import re
import time
import uuid
import os
import asyncio
from urllib.parse import unquote, urlparse

from curl_cffi.requests import AsyncSession

# Realistic User-Agent pool — each pool client gets a distinct one
USER_AGENTS = [
    # Chrome — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    # Chrome — Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Chrome — Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Firefox — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    # Firefox — Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
    # Firefox — Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    # Edge — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    # Edge — Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    # Safari — Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
]


def generate_offline_threading_id() -> str:
    """Generate an offline threading ID matching Meta's format."""
    max_int = (1 << 64) - 1
    mask22 = (1 << 22) - 1
    timestamp = int(time.time() * 1000)
    random_value = int.from_bytes(os.urandom(8), "big")
    return str(((timestamp << 22) | (random_value & mask22)) & max_int)


def extract_value(text: str, start_str: str, end_str: str) -> str:
    """Extract a value from text between start_str and end_str."""
    start = text.find(start_str)
    if start == -1:
        return ""
    value_start = start + len(start_str)
    end = text.find(end_str, value_start)
    if end == -1:
        return ""
    return text[value_start:end]


class MetaAIClient:
    def __init__(self, proxy: str | None = None, client_id: int | None = None):
        self.proxy = proxy
        self.client_id = client_id
        self.log_prefix = (
            f"[meta-client:{client_id}]" if client_id is not None else "[meta-client]"
        )
        self.cookies: dict | None = None
        self.access_token: str | None = None
        self.user_agent = (
            USER_AGENTS[client_id % len(USER_AGENTS)]
            if client_id is not None
            else USER_AGENTS[0]
        )
        self._session: AsyncSession | None = None

    async def _get_session(self) -> AsyncSession:
        """Get or create a curl_cffi async session with Chrome TLS impersonation."""
        if self._session is None:
            self._session = AsyncSession(
                impersonate="chrome110",
                proxy=self.proxy,
                timeout=90,
            )
        return self._session

    async def close(self):
        """Close the underlying HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def get_cookies(self) -> dict:
        """
        Fetch cookies from the Meta AI homepage.
        Uses curl_cffi with Chrome TLS impersonation to bypass fingerprinting.
        """
        print(f"{self.log_prefix} Fetching cookies from meta.ai...")
        session = await self._get_session()

        resp = await session.get(
            "https://www.meta.ai/",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
            allow_redirects=True,
        )

        print(f"{self.log_prefix} HTTP status: {resp.status_code}, HTML size: {len(resp.text)} bytes")

        # Handle JavaScript bot-detection challenge (403 with /__rd_verify_ endpoint)
        if resp.status_code == 403 and "/__rd_verify_" in resp.text:
            challenge_match = re.search(r"fetch\('(/__rd_verify_[^']+)'", resp.text)
            if not challenge_match:
                raise Exception(
                    f"Got 403 with challenge page but could not extract challenge path: {resp.text[:500]}"
                )
            challenge_path = challenge_match.group(1)
            print(f"{self.log_prefix} Solving bot-detection challenge: {challenge_path}")

            await session.post(
                f"https://www.meta.ai{challenge_path}",
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "*/*",
                    "Origin": "https://www.meta.ai",
                    "Referer": "https://www.meta.ai/",
                },
            )

            # Retry the original homepage request
            resp = await session.get(
                "https://www.meta.ai/",
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Cache-Control": "no-cache",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
                allow_redirects=True,
            )
            print(f"{self.log_prefix} Post-challenge HTTP status: {resp.status_code}, HTML size: {len(resp.text)} bytes")

        if resp.status_code != 200:
            raise Exception(
                f"Got HTTP {resp.status_code} from meta.ai: {resp.text[:500]}"
            )

        html = resp.text

        self.cookies = {
            "_js_datr": extract_value(html, '_js_datr":{"value":"', '",'),
            "abra_csrf": extract_value(html, 'abra_csrf":{"value":"', '",'),
            "datr": extract_value(html, 'datr":{"value":"', '",'),
            "lsd": extract_value(html, '"LSD",[],{"token":"', '"}'),
        }

        # Extract fb_dtsg if available
        fb_dtsg = extract_value(html, '"DTSGInitData",[],{"token":"', '"')
        if fb_dtsg:
            self.cookies["fb_dtsg"] = fb_dtsg

        if not self.cookies["lsd"]:
            raise Exception(
                "Failed to extract LSD token from Meta AI homepage. "
                "Bot detection likely triggered. Try with a proxy."
            )

        cookie_preview = {
            k: (v[:10] + "..." if v else "(empty)")
            for k, v in self.cookies.items()
        }
        print(f"{self.log_prefix} Extracted cookies: {cookie_preview}")

        return self.cookies

    async def get_access_token(self) -> str:
        """Get an anonymous access token by accepting TOS as a temp user."""
        if not self.cookies:
            await self.get_cookies()

        session = await self._get_session()

        cookie_str = "; ".join(
            [
                f"_js_datr={self.cookies['_js_datr']}",
                f"datr={self.cookies['datr']}",
                f"abra_csrf={self.cookies['abra_csrf']}",
                "ps_n=1",
                "ps_l=1",
                "dpr=2",
            ]
        )

        resp = await session.post(
            "https://www.meta.ai/api/graphql/",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://www.meta.ai",
                "Referer": "https://www.meta.ai/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Cookie": cookie_str,
                "X-Fb-Friendly-Name": "useAbraAcceptTOSForTempUserMutation",
                "X-Fb-Lsd": self.cookies["lsd"],
                "X-Asbd-Id": "129477",
            },
            data={
                "lsd": self.cookies["lsd"],
                "fb_dtsg": self.cookies.get("fb_dtsg", ""),
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "useAbraAcceptTOSForTempUserMutation",
                "variables": json.dumps({"dob": "1999-01-01", "tos_accepted": True}),
                "doc_id": "7604648749596940",
            },
        )

        if resp.status_code != 200:
            raise Exception(f"Token request failed: {resp.status_code} {resp.text[:500]}")

        text = resp.text

        # Response may have multiple JSON objects on separate lines (NDJSON)
        data = None
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if "data" in parsed:
                    data = parsed
                    break
            except json.JSONDecodeError:
                continue

        if not data:
            raise Exception(f"Failed to parse access token response: {text[:500]}")

        try:
            self.access_token = (
                data["data"]["xab_abra_accept_terms_of_service"]
                ["new_temp_user_auth"]["access_token"]
            )
        except (KeyError, TypeError) as e:
            raise Exception(
                f"Failed to extract access token: {e}\n"
                f"Response: {json.dumps(data)[:500]}"
            )

        print(f"{self.log_prefix} Got access token: {self.access_token[:20]}...")
        return self.access_token

    async def ensure_session(self):
        """Ensure we have valid cookies and access token. Retries once on failure."""
        try:
            if not self.cookies:
                await self.get_cookies()
            if not self.access_token:
                await self.get_access_token()
        except Exception as err:
            print(f"{self.log_prefix} Session setup failed, retrying: {err}")
            self.reset_session()
            await self.get_cookies()
            await self.get_access_token()

    async def _fire_message(self, prompt: str) -> str:
        """
        Fire a single GraphQL sendMessage call and return the raw response text.
        Does NOT handle retries or session refresh.
        """
        session = await self._get_session()
        conversation_id = str(uuid.uuid4())
        threading_id = generate_offline_threading_id()

        variables = {
            "message": {"sensitive_string_value": prompt},
            "externalConversationId": conversation_id,
            "offlineThreadingId": threading_id,
            "suggestedPromptIndex": None,
            "flashVideoRecapInput": {"images": []},
            "flashPreviewInput": None,
            "promptPrefix": None,
            "entrypoint": "ABRA__CHAT__TEXT",
            "icebreaker_type": "TEXT",
        }

        resp = await session.post(
            "https://graph.meta.ai/graphql?locale=user",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://www.meta.ai",
                "Referer": "https://www.meta.ai/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "Authorization": f"OAuth {self.access_token}",
            },
            data={
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "useAbraSendMessageMutation",
                "variables": json.dumps(variables),
                "server_timestamps": "true",
                "doc_id": "7783822248314888",
            },
        )

        if resp.status_code != 200:
            raise Exception(
                f"Send message failed: {resp.status_code} {resp.text[:500]}"
            )

        return resp.text

    def _is_session_exhausted(self, response_text: str) -> bool:
        """Check if a response indicates an exhausted/invalid session."""
        return len(response_text) < 1000 and (
            "missing_required_variable_value" in response_text
            or '"bot_response_message":null' in response_text
        )

    async def send_message(self, prompt: str) -> dict:
        """
        Send a message to Meta AI and return the parsed response.
        If the session is exhausted, automatically refreshes and retries once.
        """
        await self.ensure_session()

        response_text = await self._fire_message(prompt)

        # Detect exhausted token — refresh session and retry once
        if self._is_session_exhausted(response_text):
            print(f"{self.log_prefix} Session exhausted, refreshing token and retrying...")
            self.reset_session()
            await self.ensure_session()
            response_text = await self._fire_message(prompt)

            if self._is_session_exhausted(response_text):
                raise Exception("Session exhausted even after refresh")

        lines = [l for l in response_text.split("\n") if l.strip()]
        print(
            f"{self.log_prefix} Raw response: {len(response_text)} bytes, "
            f"lines: {len(lines)}"
        )

        parsed = self.parse_response(response_text)

        if not parsed["text"]:
            print(f"{self.log_prefix} WARNING: Empty parsed text. Raw preview:")
            print(response_text[:1000])

        # Fetch real source URLs if a fetch_id is available
        if parsed.get("fetch_id"):
            try:
                sources = await self.fetch_sources(parsed["fetch_id"])
                if sources:
                    parsed["raw_sources"] = sources
            except Exception as err:
                print(f"{self.log_prefix} Failed to fetch sources: {err}")

        return parsed

    async def fetch_sources(self, fetch_id: str) -> list:
        """Fetch real source URLs using the fetch_id from a response."""
        session = await self._get_session()

        resp = await session.post(
            "https://graph.meta.ai/graphql?locale=user",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://www.meta.ai",
                "Referer": "https://www.meta.ai/",
                "Authorization": f"OAuth {self.access_token}",
            },
            data={
                "access_token": self.access_token,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "AbraSearchPluginDialogQuery",
                "variables": json.dumps({"abraMessageFetchID": fetch_id}),
                "server_timestamps": "true",
                "doc_id": "6946734308765963",
            },
        )

        if resp.status_code != 200:
            raise Exception(f"Fetch sources failed: {resp.status_code}")

        data = resp.json()
        references = (
            (data.get("data", {}).get("message", {}) or {})
            .get("searchResults", {})
            or (data.get("data", {}).get("message", {}) or {})
            .get("search_results", {})
            or {}
        ).get("references", [])

        return [
            {
                "url": ref.get("url") or ref.get("link", ""),
                "label": ref.get("title") or ref.get("name", ""),
                "description": ref.get("snippet") or ref.get("description", ""),
            }
            for ref in references
        ]

    def parse_response(self, response_text: str) -> dict:
        """Parse the NDJSON streaming response from Meta AI."""
        last_valid_response = None
        raw_sources = []
        fetch_id = None

        for line in response_text.split("\n"):
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Extract bot response message from streamed or initial response
            bot_msg = None
            if isinstance(data.get("data"), dict):
                node = data["data"].get("node")
                if isinstance(node, dict):
                    bot_msg = node.get("bot_response_message")
                if not bot_msg:
                    send_msg = data["data"].get("xfb_abra_send_message")
                    if isinstance(send_msg, dict):
                        bot_msg = send_msg.get("bot_response_message")

            if bot_msg and isinstance(bot_msg, dict):
                content_list = (bot_msg.get("composed_text") or {}).get("content", [])
                if content_list:
                    last_valid_response = bot_msg
                if bot_msg.get("fetch_id"):
                    fetch_id = bot_msg["fetch_id"]

            # Look for source references
            search_results = None
            if isinstance(data.get("data"), dict):
                node = data["data"].get("node")
                if isinstance(node, dict):
                    search_results = node.get("search_results")
                if not search_results:
                    send_msg = data["data"].get("xfb_abra_send_message")
                    if isinstance(send_msg, dict):
                        bot_resp = send_msg.get("bot_response_message")
                        if isinstance(bot_resp, dict):
                            search_results = bot_resp.get("search_results")

            if search_results and isinstance(search_results, dict):
                for ref in search_results.get("references", []):
                    raw_sources.append(
                        {
                            "url": ref.get("url", ""),
                            "label": ref.get("title", ""),
                            "description": ref.get("snippet", ""),
                        }
                    )

        if not last_valid_response:
            return {"text": "", "raw_sources": [], "fetch_id": None}

        # Build full text from composed_text.content
        text_parts = [
            c.get("text", "")
            for c in (last_valid_response.get("composed_text") or {}).get(
                "content", []
            )
        ]
        full_text = "\n".join(text_parts)

        # Extract inline sources if no structured sources found
        if not raw_sources:
            raw_sources = self._extract_inline_sources(full_text)

        return {"text": full_text, "raw_sources": raw_sources, "fetch_id": fetch_id}

    def _extract_inline_sources(self, text: str) -> list:
        """Extract source URLs from Meta AI's inline source references."""
        sources = []
        pattern = r"https?://l\.meta\.ai/\?u=([^&\s]+)"
        for match in re.finditer(pattern, text):
            decoded_url = unquote(match.group(1))
            try:
                hostname = urlparse(decoded_url).hostname or decoded_url
            except Exception:
                hostname = decoded_url
            sources.append(
                {"url": decoded_url, "label": hostname, "description": ""}
            )
        return sources

    async def prompt(self, text: str) -> dict:
        """
        Send a prompt to Meta AI and return a structured response.
        Sends without system prompt to ensure real source URLs are returned,
        then structures the response server-side.
        """
        raw_response = await self.send_message(text)
        response_text = raw_response.get("text", "")
        raw_sources = raw_response.get("raw_sources", [])
        return self.build_structured_response(response_text, raw_sources)

    def build_structured_response(self, text: str, raw_sources: list) -> dict:
        """Build the structured response from raw text."""
        clean_text = text.strip()
        markdown_text = clean_text
        html_text = ""

        sources = [
            {
                "position": i,
                "url": src.get("url", ""),
                "label": src.get("label", ""),
                "description": src.get("description", ""),
            }
            for i, src in enumerate(raw_sources)
        ]

        return {
            "success": True,
            "result": {
                "text": clean_text,
                "sources": sources,
                "html": html_text,
                "markdown": markdown_text,
                "searchQueries": [],
                "shoppingCards": [],
                "model": "meta-ai",
            },
        }

    def reset_session(self):
        """Reset session to force re-authentication."""
        self.cookies = None
        self.access_token = None
