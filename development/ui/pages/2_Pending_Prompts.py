"""Pending Prompts page — approve or deny T3/T4 prompts awaiting decision."""

import streamlit as st

from ui.data import approve_prompt, get_pending_prompts

# Header row: title on left, refresh button on right
hcol1, hcol2 = st.columns([4, 1])
with hcol1:
    st.title("\U0001f514 Pending Prompts")
with hcol2:
    if st.button("🔄 Refresh list", use_container_width=True):
        st.rerun()

st.caption("Updates after each approve/deny action.")


def _act(pid: str, decision: str, reason: str, promote_to_tier: int | None = None) -> None:
    try:
        approve_prompt(pid, decision, reason, promote_to_tier=promote_to_tier)
        st.toast(f"{decision} → {pid[:8]}…", icon="✅")
        st.rerun()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Failed: {exc}")


prompts = get_pending_prompts()
if not prompts:
    st.success("No pending prompts.")
    st.stop()

st.write(f"**{len(prompts)} prompt(s) awaiting decision**")

for p in prompts:
    pid = p["id"]
    with st.container(border=True):
        cols = st.columns([2, 1])
        with cols[0]:
            st.markdown(f"**Prompt** `{pid}`")
            st.code(p.get("raw_cmd", ""), language="bash")
            st.caption(
                f"tier T{p.get('command_tier', '?')} · "
                f"agent `{p.get('agent_id', '?')}` · "
                f"cwd `{p.get('cwd', '?')}`"
            )
        with cols[1]:
            reason = st.text_input("Reason (optional)", key=f"reason_{pid}")
            tier_label = st.selectbox(
                "Promote to tier (templates only)",
                options=["T2 (auto-execute, logged)", "T1 (auto-execute, silent)"],
                index=0,
                key=f"tier_{pid}",
                help="Applies only to 'Approve template' and 'Approve global'. Ignored for 'Approve once'.",
            )
            promote_to_tier = 2 if tier_label.startswith("T2") else 1

            b1, b2, b3, b4 = st.columns(4)
            if b1.button("Approve once", key=f"once_{pid}", type="primary"):
                _act(pid, "approve_once", reason)
            if b2.button("Approve template", key=f"tmpl_{pid}", help="Permanent for THIS agent only"):
                _act(pid, "approve_template", reason, promote_to_tier=promote_to_tier)
            if b3.button("Approve global", key=f"global_{pid}", help="Permanent for ALL agents"):
                _act(pid, "approve_template_global", reason, promote_to_tier=promote_to_tier)
            if b4.button("Deny", key=f"deny_{pid}"):
                _act(pid, "deny", reason)
