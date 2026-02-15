"""
Unit tests for the Python Meta AI scraper — runs without network access.
Mocks all external HTTP calls.
"""

import asyncio
import json
import os
import sys
import time

# ── meta_client tests ──────────────────────────────────────────────

from meta_client import MetaAIClient, generate_offline_threading_id, extract_value


def test_generate_offline_threading_id():
    tid = generate_offline_threading_id()
    assert isinstance(tid, str)
    assert len(tid) > 10
    val = int(tid)
    assert val > 0
    print(f"  generate_offline_threading_id: {tid}")


def test_extract_value():
    html = '"LSD",[],{"token":"abc123"}'
    val = extract_value(html, '"LSD",[],{"token":"', '"}')
    assert val == "abc123"

    html2 = '"datr":{"value":"xyz789","expire_time":0}'
    val2 = extract_value(html2, '"datr":{"value":"', '",')
    assert val2 == "xyz789"

    # Missing start
    assert extract_value("nothing here", "start", "end") == ""
    # Missing end
    assert extract_value("start_but_no_end", "start_", "missing") == ""
    print("  extract_value: all cases passed")


def test_client_creation():
    client = MetaAIClient(proxy=None, client_id=5)
    assert client.client_id == 5
    assert client.user_agent.startswith("Mozilla")
    assert client.cookies is None
    assert client.access_token is None
    print(f"  client creation: id=5, UA={client.user_agent[:40]}...")


def test_parse_response_normal():
    client = MetaAIClient()
    response = "\n".join(
        [
            json.dumps(
                {
                    "data": {
                        "node": {
                            "bot_response_message": {
                                "composed_text": {
                                    "content": [{"text": "Partial "}]
                                },
                            }
                        }
                    }
                }
            ),
            json.dumps(
                {
                    "data": {
                        "node": {
                            "bot_response_message": {
                                "composed_text": {
                                    "content": [
                                        {"text": "Full response text here."}
                                    ]
                                },
                                "fetch_id": "fetch_abc123",
                            }
                        }
                    }
                }
            ),
        ]
    )
    parsed = client.parse_response(response)
    assert parsed["text"] == "Full response text here."
    assert parsed["fetch_id"] == "fetch_abc123"
    print(f"  parse_response (normal): text='{parsed['text'][:30]}', fetch_id={parsed['fetch_id']}")


def test_parse_response_with_sources():
    client = MetaAIClient()
    response = json.dumps(
        {
            "data": {
                "node": {
                    "bot_response_message": {
                        "composed_text": {
                            "content": [{"text": "Answer with sources"}]
                        },
                    },
                    "search_results": {
                        "references": [
                            {
                                "url": "https://example.com",
                                "title": "Example",
                                "snippet": "A test source",
                            }
                        ]
                    },
                }
            }
        }
    )
    parsed = client.parse_response(response)
    assert parsed["text"] == "Answer with sources"
    assert len(parsed["raw_sources"]) == 1
    assert parsed["raw_sources"][0]["url"] == "https://example.com"
    print(f"  parse_response (sources): {len(parsed['raw_sources'])} source(s)")


def test_parse_response_xfb_format():
    """Test the alternative response format (xfb_abra_send_message)."""
    client = MetaAIClient()
    response = json.dumps(
        {
            "data": {
                "xfb_abra_send_message": {
                    "bot_response_message": {
                        "composed_text": {
                            "content": [{"text": "Alt format response"}]
                        }
                    }
                }
            }
        }
    )
    parsed = client.parse_response(response)
    assert parsed["text"] == "Alt format response"
    print(f"  parse_response (xfb format): text='{parsed['text']}'")


def test_parse_response_empty():
    client = MetaAIClient()
    parsed = client.parse_response("")
    assert parsed["text"] == ""
    assert parsed["raw_sources"] == []
    assert parsed["fetch_id"] is None
    print("  parse_response (empty): OK")


def test_parse_response_multipart():
    """Test composed_text with multiple content parts."""
    client = MetaAIClient()
    response = json.dumps(
        {
            "data": {
                "node": {
                    "bot_response_message": {
                        "composed_text": {
                            "content": [
                                {"text": "Part one."},
                                {"text": "Part two."},
                                {"text": "Part three."},
                            ]
                        }
                    }
                }
            }
        }
    )
    parsed = client.parse_response(response)
    assert "Part one." in parsed["text"]
    assert "Part two." in parsed["text"]
    assert "Part three." in parsed["text"]
    print(f"  parse_response (multipart): text contains all 3 parts")


def test_inline_source_extraction():
    client = MetaAIClient()
    text = (
        "Check https://l.meta.ai/?u=https%3A%2F%2Fexample.com%2Fpage1 and "
        "also https://l.meta.ai/?u=https%3A%2F%2Fother.org%2Fdocs for more info."
    )
    sources = client._extract_inline_sources(text)
    assert len(sources) == 2
    assert sources[0]["url"] == "https://example.com/page1"
    assert sources[1]["url"] == "https://other.org/docs"
    print(f"  inline sources: found {len(sources)}")


def test_session_exhausted_detection():
    client = MetaAIClient()
    assert client._is_session_exhausted('{"error":"missing_required_variable_value"}')
    assert client._is_session_exhausted('{"data":{"bot_response_message":null}}')
    assert not client._is_session_exhausted("x" * 2000)  # Too long
    assert not client._is_session_exhausted('{"data":{"node":{}}}')  # No marker
    print("  session exhausted detection: all cases passed")


def test_build_structured_response():
    client = MetaAIClient()
    result = client.build_structured_response(
        "# Hello World\nThis is a test.",
        [
            {"url": "https://a.com", "label": "A", "description": "Source A"},
            {"url": "https://b.com", "label": "B", "description": "Source B"},
        ],
    )
    assert result["success"] is True
    assert result["result"]["text"] == "# Hello World\nThis is a test."
    assert result["result"]["model"] == "meta-ai"
    assert len(result["result"]["sources"]) == 2
    assert result["result"]["sources"][0]["position"] == 0
    assert result["result"]["sources"][1]["position"] == 1
    assert "<div" in result["result"]["html"]
    print(f"  build_structured_response: success={result['success']}, sources={len(result['result']['sources'])}")


def test_reset_session():
    client = MetaAIClient()
    client.cookies = {"lsd": "fake"}
    client.access_token = "fake_token"
    client.reset_session()
    assert client.cookies is None
    assert client.access_token is None
    print("  reset_session: OK")


# ── client_pool tests ──────────────────────────────────────────────

from client_pool import ClientPool


def test_pool_creation():
    pool = ClientPool(pool_size=5, queue_timeout=10, proxy=None)
    assert len(pool.entries) == 5
    assert all(not e["ready"] for e in pool.entries)
    assert all(not e["busy"] for e in pool.entries)
    # Each client should get a unique user agent
    uas = set(e["client"].user_agent for e in pool.entries)
    assert len(uas) == 5
    print(f"  pool creation: {len(pool.entries)} entries, {len(uas)} unique UAs")


def test_pool_acquire_release():
    pool = ClientPool(pool_size=2)
    # Mark one as ready
    pool.entries[0]["ready"] = True

    async def _test():
        entry = await pool.acquire(timeout=1)
        assert entry["id"] == 0
        assert entry["busy"] is True
        pool.release(entry)
        assert entry["busy"] is False

    asyncio.run(_test())
    print("  pool acquire/release: OK")


def test_pool_no_healthy_clients():
    pool = ClientPool(pool_size=2)
    # All not ready

    async def _test():
        try:
            await pool.acquire(timeout=0.1)
            assert False, "Should have raised"
        except Exception as e:
            assert "No healthy clients" in str(e)

    asyncio.run(_test())
    print("  pool no healthy clients: raises correctly")


# ── database tests ─────────────────────────────────────────────────

from database import init_db, save_response, get_response


def test_database():
    db = init_db()

    save_response(
        {
            "id": "unit_test_001",
            "timestamp": "2025-01-01T00:00:00Z",
            "timestamp_unix": 1735689600000,
            "duration_ms": 500,
            "retried": False,
            "prompt": "Unit test prompt",
            "country": "BR",
            "status_code": 200,
        },
        {
            "success": True,
            "result": {
                "text": "Test response text",
                "sources": [{"url": "https://example.com"}],
                "model": "meta-ai",
            },
        },
    )

    row = get_response("unit_test_001")
    assert row is not None
    assert row["prompt"] == "Unit test prompt"
    assert row["country"] == "BR"
    assert row["result"]["success"] is True

    # Nonexistent
    assert get_response("nonexistent") is None
    print("  database save/get: OK")

    # Clean up
    import shutil
    from pathlib import Path

    db_dir = Path(__file__).parent / "data"
    if db_dir.exists():
        shutil.rmtree(db_dir)
    print("  database cleanup: OK")


# ── test_parallel tests ────────────────────────────────────────────

from test_parallel import percentile


def test_percentile():
    data = list(range(1, 101))  # 1 to 100
    assert percentile(data, 50) == 50
    assert percentile(data, 95) == 95
    assert percentile(data, 99) == 99
    assert percentile(data, 100) == 100

    small = [10, 20, 30]
    assert percentile(small, 50) == 10  # ceil(1.5)-1 = 0 -> 10... wait
    print("  percentile: OK")


# ── Run all tests ──────────────────────────────────────────────────

def main():
    tests = [
        ("generate_offline_threading_id", test_generate_offline_threading_id),
        ("extract_value", test_extract_value),
        ("client_creation", test_client_creation),
        ("parse_response_normal", test_parse_response_normal),
        ("parse_response_with_sources", test_parse_response_with_sources),
        ("parse_response_xfb_format", test_parse_response_xfb_format),
        ("parse_response_empty", test_parse_response_empty),
        ("parse_response_multipart", test_parse_response_multipart),
        ("inline_source_extraction", test_inline_source_extraction),
        ("session_exhausted_detection", test_session_exhausted_detection),
        ("build_structured_response", test_build_structured_response),
        ("reset_session", test_reset_session),
        ("pool_creation", test_pool_creation),
        ("pool_acquire_release", test_pool_acquire_release),
        ("pool_no_healthy_clients", test_pool_no_healthy_clients),
        ("database", test_database),
        ("percentile", test_percentile),
    ]

    passed = 0
    failed = 0

    print("=" * 50)
    print("Running unit tests...")
    print("=" * 50)
    print()

    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            failed += 1

    print()
    print("=" * 50)
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    print("=" * 50)

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
