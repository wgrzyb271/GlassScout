"""Live discovery progress, separate from editable review forms."""

from math import ceil

import streamlit as st


def duration_label(seconds: float) -> str:
    minutes, seconds = divmod(max(0, ceil(seconds)), 60)
    return f"{minutes:02d}:{seconds:02d}"


def render_progress(record: dict, running: bool) -> None:
    status = record["status"]
    if status == "running" and not running:
        status = "interrupted — application restarted"
    st.write(f"{record['phase']} · {status}")
    st.caption(record["detail"])

    remaining = record.get("remaining_seconds")
    limit = record.get("timeout_seconds", 0)
    if running and status == "running" and remaining is not None and limit > 0:
        st.progress(min(1.0, max(0.0, 1 - remaining / limit)), text=f"Agent timeout in {duration_label(remaining)}")
        st.caption(f"Run time limit: {duration_label(limit)} · bar shows time used, not scan completion.")
        if remaining <= 0:
            st.caption("Time limit reached — finishing the bounded request and saving partial results.")

    scan_remaining = record.get("scan_remaining_seconds")
    scan_limit = record.get("scan_timeout_seconds", 0)
    if running and status == "running" and scan_remaining is not None and scan_limit > 0:
        st.progress(min(1.0, max(0.0, 1 - scan_remaining / scan_limit)), text=f"TCP scan timeout in {duration_label(scan_remaining)}")
        st.caption("TCP scan time budget used. Remaining run time is reserved for identifying services.")

    total = record["endpoints"]
    if total:
        st.progress(min(max(record["processed"] / total, 0.0), 1.0), text=f"{record['processed']} / {total} addresses checked · {record['added']} added")
    elif running:
        st.caption("Discovering candidate addresses…")
