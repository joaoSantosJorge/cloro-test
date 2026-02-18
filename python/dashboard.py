"""Streamlit dashboard for Meta AI scraper results.

Launch:
    streamlit run python/dashboard.py
"""

import json
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "meta-ai.db"

st.set_page_config(page_title="Meta AI Scraper Dashboard", layout="wide")
st.title("Meta AI Scraper — Results Dashboard")


@st.cache_data(ttl=30)
def load_data():
    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        "SELECT id, timestamp, success, duration_ms, text_length, "
        "source_count, model, result_json FROM responses",
        conn,
    )
    conn.close()
    df["success"] = df["success"].astype(bool)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df


df = load_data()

if df.empty:
    st.warning("No results found in the database.")
    st.stop()

# ── Summary cards ────────────────────────────────────────────────────────────
total = len(df)
ok = df["success"].sum()
fail = total - ok
rate = 100 * ok / total if total else 0
avg_dur = df["duration_ms"].mean()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Requests", total)
c2.metric("Successes", int(ok))
c3.metric("Failures", int(fail))
c4.metric("Success Rate", f"{rate:.1f}%")
c5.metric("Avg Duration", f"{avg_dur:,.0f} ms")

st.divider()

# ── Charts ───────────────────────────────────────────────────────────────────
chart1, chart2, chart3 = st.columns(3)

with chart1:
    st.subheader("Success / Fail")
    pie_df = pd.DataFrame(
        {"status": ["Success", "Fail"], "count": [int(ok), int(fail)]}
    )
    st.bar_chart(pie_df.set_index("status"))

with chart2:
    st.subheader("Duration Distribution (ms)")
    st.bar_chart(df["duration_ms"].value_counts(bins=20).sort_index())

with chart3:
    st.subheader("Success Rate Over Time")
    df_sorted = df.sort_values("timestamp").reset_index(drop=True)
    # Rolling success rate over a window of 10
    window = min(10, len(df_sorted))
    df_sorted["rolling_rate"] = (
        df_sorted["success"].rolling(window, min_periods=1).mean() * 100
    )
    st.line_chart(df_sorted["rolling_rate"])

st.divider()

# ── Filters ──────────────────────────────────────────────────────────────────
st.subheader("Results Table")

col_f1, col_f2 = st.columns(2)
with col_f1:
    status_filter = st.selectbox("Filter by status", ["All", "Success", "Fail"])
with col_f2:
    search_text = st.text_input("Search in result JSON")

filtered = df.copy()
if status_filter == "Success":
    filtered = filtered[filtered["success"]]
elif status_filter == "Fail":
    filtered = filtered[~filtered["success"]]

if search_text:
    mask = filtered["result_json"].str.contains(search_text, case=False, na=False)
    filtered = filtered[mask]

# Display table without result_json (too wide); show it in expanders below
display_cols = [c for c in filtered.columns if c != "result_json"]
st.dataframe(filtered[display_cols], use_container_width=True)

# ── Expandable full JSON ─────────────────────────────────────────────────────
st.subheader("Full Response JSON")
for _, row in filtered.iterrows():
    label = f"{'OK' if row['success'] else 'FAIL'} — {row['id']} — {row['duration_ms']}ms"
    with st.expander(label):
        try:
            st.json(json.loads(row["result_json"]))
        except (json.JSONDecodeError, TypeError):
            st.code(row["result_json"])
