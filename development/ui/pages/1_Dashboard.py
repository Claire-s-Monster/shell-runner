"""Dashboard page — live health stats and recent calls."""

import pandas as pd
import streamlit as st

from ui.data import get_health, get_recent_calls

st.title("\U0001f4ca Dashboard")

# Auto-refresh every 10s
st.checkbox("Auto-refresh (10s)", key="auto_refresh", value=True)
if st.session_state.auto_refresh:
    st.markdown('<meta http-equiv="refresh" content="10">', unsafe_allow_html=True)

health = get_health()
if health.get("status") != "ok":
    st.error(f"Daemon unreachable: {health.get('error', health)}")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("calls (24h)", health.get("total_calls_24h", "—"))
c2.metric("denied rate", f"{health.get('denied_rate_24h', 0) * 100:.3f}%")
c3.metric("prompt rate", f"{health.get('prompt_rate_24h', 0) * 100:.2f}%")
c4.metric("p99 latency", f"{health.get('p99_latency_ms', '—')} ms")

st.divider()

st.subheader("Recent calls")
limit = st.slider("rows", 10, 200, 50)
calls = get_recent_calls(limit=limit)
if not calls:
    st.info("No calls yet.")
else:
    df = pd.DataFrame(calls)
    # Show a useful subset; defensive about column names.
    # Actual columns: ts, command_tier, final_tier, decision, agent_id, cwd,
    #                 normalized_template, exit_code, duration_ms
    preferred = [
        "ts",
        "command_tier",
        "final_tier",
        "decision",
        "agent_id",
        "cwd",
        "normalized_template",
        "exit_code",
        "duration_ms",
    ]
    cols = [c for c in preferred if c in df.columns]
    st.dataframe(df[cols] if cols else df, use_container_width=True, hide_index=True)
