"""Pending Prompts page — approve/deny T3/T4 prompts, explain why, propose rule refinements."""

import os
from pathlib import Path

import streamlit as st

from ui.data import (
    approve_prompt,
    get_pending_prompts,
    get_similar_approvals,
    parse_decision_path,
)
from ui.refinement import (
    AnalysisError,
    build_analysis_prompt,
    file_github_issue,
    redact,
    run_claude_analysis,
)

REPO_DIR = Path(
    os.environ.get("SHELL_RUNNER_REPO_DIR", str(Path(__file__).resolve().parents[2]))
)
CLAUDE_BIN = os.environ.get("SHELL_RUNNER_CLAUDE_BIN", "claude")
ANALYSIS_TIMEOUT_S = int(os.environ.get("SHELL_RUNNER_ANALYSIS_TIMEOUT_S", "180"))
GH_REPO = os.environ.get("SHELL_RUNNER_GH_REPO", "Claire-s-Monster/shell-runner")
GH_TOKEN = os.environ.get("SHELL_RUNNER_GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


hcol1, hcol2 = st.columns([4, 1])
with hcol1:
    st.title("\U0001f514 Pending Prompts")
with hcol2:
    if st.button("🔄 Refresh list", use_container_width=True):
        st.rerun()

st.caption("Updates after each approve/deny action.")


def _act(pid, decision, reason, promote_to_tier=None):
    try:
        approve_prompt(pid, decision, reason, promote_to_tier=promote_to_tier)
        st.toast(f"{decision} → {pid[:8]}…", icon="✅")
        st.rerun()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Failed: {exc}")


def _render_reasoning(p):
    cat = p.get("matched_rule_category") or "no rule matched (T3 fallthrough)"
    st.caption(f"matched: {cat}")
    steps = parse_decision_path(p.get("decision_path_json"))
    if steps:
        with st.expander("Why this needs approval"):
            for step in steps:
                st.markdown(f"- {step}")
    similar = get_similar_approvals(
        p.get("normalized_template", ""), p.get("agent_id", "")
    )
    with st.expander(f"Similar past approvals ({len(similar)})"):
        if not similar:
            st.write("None found.")
        for s in similar:
            ex = s.get("example_raw_cmd")
            shown = redact(ex) if ex else "(no example)"
            st.markdown(f"- sim={s['similarity']} · T{s['approved_tier']} · `{shown}`")


def _render_refinement(p):
    pid = p["id"]
    state_key = f"proposal_{pid}"
    if st.button("🔍 Propose rule enhancement", key=f"analyze_{pid}"):
        try:
            prompt = build_analysis_prompt(
                raw_cmd=p.get("raw_cmd", ""),
                normalized_template=p.get("normalized_template", ""),
                command_tier=p.get("command_tier", 0),
                matched_rule_category=p.get("matched_rule_category"),
                decision_path=parse_decision_path(p.get("decision_path_json")),
                similar=get_similar_approvals(
                    p.get("normalized_template", ""), p.get("agent_id", "")
                ),
            )
            with st.spinner("Analyzing in a read-only Claude session… (up to ~3 min)"):
                st.session_state[state_key] = run_claude_analysis(
                    prompt, REPO_DIR, claude_bin=CLAUDE_BIN, timeout_s=ANALYSIS_TIMEOUT_S
                )
        except AnalysisError as exc:
            st.session_state.pop(state_key, None)
            st.error(f"Analysis failed: {exc}")

    proposal = st.session_state.get(state_key)
    if not proposal:
        return
    with st.container(border=True):
        st.markdown(
            f"**Proposed:** {proposal['issue_title']}  ·  confidence {proposal['confidence']}"
        )
        st.markdown(f"**Why it missed:** {proposal['missed_reason']}")
        if proposal.get("proposed_rule"):
            st.markdown(f"**New rule** in `{proposal['catalog_section']}`:")
            st.code(proposal["proposed_rule"]["pattern"], language="text")
        else:
            st.markdown(f"**Recommends existing lever:** `{proposal['existing_lever']}`")
        st.caption(f"Risk: {proposal['risk_notes']}")

        redacted_preview = redact(p.get("raw_cmd", ""))
        st.markdown("**Command that will be posted (redacted):**")
        st.code(redacted_preview, language="bash")

        fcol, dcol = st.columns(2)
        file_disabled = GH_TOKEN is None
        if fcol.button(
            "📋 File GitHub issue",
            key=f"file_{pid}",
            disabled=file_disabled,
            help=(
                "Set SHELL_RUNNER_GH_TOKEN or GITHUB_TOKEN to enable"
                if file_disabled
                else None
            ),
        ):
            try:
                res = file_github_issue(
                    proposal,
                    redacted_preview,
                    p.get("normalized_template", ""),
                    GH_REPO,
                    GH_TOKEN,
                )
                if res["status"] == "duplicate":
                    st.info(f"Already filed: {res['url']}")
                else:
                    st.success(f"Filed: {res['url']}")
                st.session_state.pop(state_key, None)
            except Exception as exc:  # noqa: BLE001
                st.error(f"File failed: {exc}")
        if dcol.button("Discard", key=f"discard_{pid}"):
            st.session_state.pop(state_key, None)
            st.rerun()


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
            _render_reasoning(p)
            _render_refinement(p)
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
