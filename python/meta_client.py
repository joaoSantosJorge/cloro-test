"""
Core Meta AI client — challenge solving, TOS acceptance, and message sending.

Uses curl_cffi for TLS-fingerprinted HTTP requests (impersonates Chrome).
Targets the new Next.js-based Meta AI API (as of March 2026) which uses:
  - /api/graphql with JSON body and doc_id-based persisted queries
  - SSE (Server-Sent Events) streaming for message responses
  - Bearer token auth obtained via acceptTOSForLoggedOut mutation
"""

import json
import logging
import os
import re
import time
import uuid

from curl_cffi.requests import AsyncSession

from constants import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_CHALLENGE_ATTEMPTS,
    THREADING_ID_RANDOM_BITS,
    THREADING_ID_TOTAL_BITS,
    TLS_IMPERSONATION_TARGET,
)
from exceptions import (
    ChallengeError,
    SendMessageError,
    SessionExhaustedError,
    TokenError,
)

logger = logging.getLogger("meta_ai.client")

# Persisted query doc_ids for the new Next.js Meta AI API
DOC_ID_ACCEPT_TOS = "38ea0730fbf07d317f0b3fd0239f1292"
DOC_ID_SEND_MESSAGE = "ce4f00a6f56e598c600a23318ab0e69c"

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
    max_int = (1 << THREADING_ID_TOTAL_BITS) - 1
    mask = (1 << THREADING_ID_RANDOM_BITS) - 1
    timestamp = int(time.time() * 1000)
    random_value = int.from_bytes(os.urandom(8), "big")
    return str(((timestamp << THREADING_ID_RANDOM_BITS) | (random_value & mask)) & max_int)


class MetaAIClient:
    def __init__(self, proxy: str | None = None, client_id: int | None = None):
        self.proxy = proxy
        self.client_id = client_id
        self.access_token: str | None = None
        self.abra_user_id: str | None = None
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
                impersonate=TLS_IMPERSONATION_TARGET,
                proxy=self.proxy,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                verify=self.proxy is None,
            )
        return self._session

    async def close(self):
        """Close the underlying HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def _solve_challenge(self) -> None:
        """
        Load the Meta AI homepage and solve bot-detection challenges.
        Sets up session cookies (datr, rd_challenge) needed for API calls.
        """
        session = await self._get_session()

        resp = await session.get(
            "https://www.meta.ai/",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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

        for _ in range(MAX_CHALLENGE_ATTEMPTS):
            if resp.status_code != 403 or "/__rd_verify_" not in resp.text:
                break

            challenge_match = re.search(r"fetch\('(/__rd_verify_[^']+)'", resp.text)
            if not challenge_match:
                raise ChallengeError(
                    f"Got 403 with challenge page but could not extract challenge path: {resp.text[:500]}"
                )
            challenge_path = challenge_match.group(1)

            await session.post(
                f"https://www.meta.ai{challenge_path}",
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "*/*",
                    "Origin": "https://www.meta.ai",
                    "Referer": "https://www.meta.ai/",
                },
            )

            resp = await session.get(
                "https://www.meta.ai/",
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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

        if resp.status_code != 200:
            raise ChallengeError(
                f"Got HTTP {resp.status_code} from meta.ai after challenge: {resp.text[:500]}"
            )

    async def get_access_token(self) -> str:
        """
        Accept TOS as an anonymous user and get a temporary access token.
        Uses the new Next.js API endpoint with acceptTOSForLoggedOut mutation.
        """
        await self._solve_challenge()

        session = await self._get_session()

        resp = await session.post(
            "https://www.meta.ai/api/graphql",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://www.meta.ai",
                "Referer": "https://www.meta.ai/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
            json={
                "doc_id": DOC_ID_ACCEPT_TOS,
                "variables": {"input": {"dateOfBirth": "1995-01-01"}},
            },
        )

        if resp.status_code != 200:
            raise TokenError(
                f"TOS acceptance failed: HTTP {resp.status_code} {resp.text[:500]}"
            )

        data = resp.json()
        accept_data = data.get("data", {}).get("acceptTOSForLoggedOut", {})

        if accept_data.get("error"):
            raise TokenError(f"TOS acceptance error: {accept_data['error']}")

        self.access_token = accept_data.get("accessToken")
        if not self.access_token:
            raise TokenError(
                f"No access token in TOS response: {json.dumps(data)[:500]}"
            )

        viewer = accept_data.get("viewer") or {}
        self.abra_user_id = viewer.get("abraUserId")

        return self.access_token

    async def ensure_session(self):
        """Ensure we have a valid access token."""
        if not self.access_token:
            await self.get_access_token()

    async def send_message(self, prompt: str) -> dict:
        """
        Send a message to Meta AI and return the parsed response.
        Uses SSE streaming via the /api/graphql endpoint.
        """
        await self.ensure_session()

        response_text = await self._fire_message(prompt)

        # Check for session exhaustion
        if len(response_text) < 200 and "error" in response_text.lower():
            # Refresh session and retry once
            self.reset_session()
            await self.ensure_session()
            response_text = await self._fire_message(prompt)

            if len(response_text) < 200 and "error" in response_text.lower():
                raise SessionExhaustedError("Session exhausted even after refresh")

        return self._parse_sse_response(response_text)

    async def _fire_message(self, prompt: str) -> str:
        """
        Send a single message via the /api/graphql SSE endpoint.
        Returns the raw SSE response text.
        """
        session = await self._get_session()

        conversation_id = str(uuid.uuid4())
        user_msg_id = str(uuid.uuid4())
        assistant_msg_id = str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        threading_id = generate_offline_threading_id()

        variables = {
            "conversationId": conversation_id,
            "content": prompt,
            "userMessageId": user_msg_id,
            "assistantMessageId": assistant_msg_id,
            "userUniqueMessageId": threading_id,
            "turnId": turn_id,
        }

        resp = await session.post(
            "https://www.meta.ai/api/graphql",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                "Origin": "https://www.meta.ai",
                "Referer": "https://www.meta.ai/",
                "Authorization": f"Bearer {self.access_token}",
                "X-Abra-User-Id": self.abra_user_id or "",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
            json={
                "doc_id": DOC_ID_SEND_MESSAGE,
                "variables": variables,
            },
        )

        if resp.status_code != 200:
            raise SendMessageError(
                f"Send message failed: HTTP {resp.status_code} {resp.text[:500]}"
            )

        return resp.text

    def _parse_sse_response(self, response_text: str) -> dict:
        """Parse the SSE streaming response from the new Meta AI API."""
        last_content = ""
        sources = []

        # SSE events are separated by double newlines
        events = response_text.split("\n\n")
        for event in events:
            for line in event.strip().split("\n"):
                if not line.startswith("data: "):
                    continue
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                msg = data.get("data", {}).get("sendMessageStream")
                if not msg:
                    continue

                content = msg.get("content", "")
                if content:
                    last_content = content

                # Extract sources from contentRenderer
                renderer = msg.get("contentRenderer") or {}
                renderer_msg = renderer.get("message") or {}
                msg_sources = renderer_msg.get("sources") or []
                if msg_sources:
                    sources = msg_sources

                # Also check unified_response for sources
                unified = renderer.get("unified_response") or {}
                unified_sources = unified.get("sources") or []
                if unified_sources:
                    sources = unified_sources

        return {
            "text": last_content,
            "raw_sources": [
                {
                    "url": src.get("url", ""),
                    "label": src.get("title") or src.get("label", ""),
                    "description": src.get("snippet") or src.get("description", ""),
                }
                for src in sources
            ],
        }

    async def prompt(self, text: str) -> dict:
        """
        Send a prompt to Meta AI and return a structured response.
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
        self.access_token = None
        self.abra_user_id = None
