"""CLI entry point for querying Meta AI."""

import argparse
import asyncio
import json
import sys

from meta_client import MetaAIClient


async def main(prompt: str, proxy: str | None = None) -> None:
    client = MetaAIClient(proxy=proxy)
    try:
        result = await client.prompt(prompt)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query Meta AI from the command line")
    parser.add_argument("prompt", help="The prompt to send to Meta AI")
    parser.add_argument("--proxy", default=None, help="HTTP proxy URL (e.g. http://host:port)")
    args = parser.parse_args()

    asyncio.run(main(args.prompt, args.proxy))
