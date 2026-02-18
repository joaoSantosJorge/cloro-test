"""Quick viewer for batch run results stored in SQLite.

Usage:
    python check_results.py            # summary + one-line per request
    python check_results.py -d         # also print full response JSON
    python check_results.py -d 5       # full JSON for request #5 only
    python check_results.py --csv      # export all results to data/results.csv
"""

import csv
import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "meta-ai.db"
CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "results.csv"


def get_preview(data, success):
    """Extract a clean text preview from the response."""
    if not success:
        return data.get("error", "")[:150]
    text = data.get("result", {}).get("text", "")
    # Meta AI sometimes returns JSON as text — try to unwrap it
    if text.startswith("{"):
        try:
            inner = json.loads(text)
            text = inner.get("result", {}).get("text", text)
        except (json.JSONDecodeError, AttributeError):
            pass
    # Collapse whitespace for preview
    return " ".join(text.split())[:150]


def export_csv(conn):
    """Export all results to CSV."""
    rows = conn.execute(
        "SELECT id, timestamp, success, duration_ms, text_length, "
        "source_count, model, result_json FROM responses"
    ).fetchall()

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "id", "timestamp", "success", "duration_ms",
            "text_length", "source_count", "model", "error_or_preview",
        ])
        for row_id, ts, success, dur, tlen, src_cnt, model, rjson in rows:
            data = json.loads(rjson)
            preview = get_preview(data, success)
            writer.writerow([
                row_id, ts, bool(success), dur, tlen, src_cnt, model, preview,
            ])

    print(f"Exported {len(rows)} rows -> {CSV_PATH}")


def main():
    conn = sqlite3.connect(str(DB_PATH))

    if "--csv" in sys.argv:
        export_csv(conn)
        conn.close()
        return

    rows = conn.execute(
        "SELECT success, text_length, duration_ms, result_json FROM responses"
    ).fetchall()

    total = len(rows)
    ok = sum(1 for r in rows if r[0])
    rate = 100 * ok / total if total else 0
    print(f"Total: {total} | OK: {ok} | Fail: {total - ok} | Rate: {rate:.1f}%\n")

    # Parse args
    detail = "-d" in sys.argv or "--detail" in sys.argv
    detail_index = None
    for arg in sys.argv[1:]:
        if arg.isdigit():
            detail_index = int(arg)

    for i, (success, text_len, duration, result_json) in enumerate(rows):
        data = json.loads(result_json)
        status = "OK" if success else "FAIL"
        preview = get_preview(data, success)
        print(f"#{i+1:>3} [{status:>4}] {duration:>6}ms {text_len:>5}chars | {preview}")

        if detail and (detail_index is None or detail_index == i + 1):
            print(json.dumps(data, indent=2))
            print()

    conn.close()


if __name__ == "__main__":
    main()
