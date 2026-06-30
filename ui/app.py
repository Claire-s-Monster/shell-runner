"""Shell-runner admin UI.

Run: pixi run --environment ui ui
URL: http://127.0.0.1:8511
"""

import streamlit as st

st.set_page_config(
    page_title="shell-runner admin",
    page_icon="\U0001f41a",
    layout="wide",
)

st.title("\U0001f41a shell-runner admin")
st.markdown(
    "Use the sidebar to navigate. **Localhost only** — no auth. "
    "If you exposed this beyond 127.0.0.1, you have bigger problems."
)
st.info(
    "Live health, recent calls, and pending T3/T4 prompts. "
    "All actions hit the running daemon at `127.0.0.1:4111`."
)
