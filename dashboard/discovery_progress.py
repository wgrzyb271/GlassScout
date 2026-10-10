"""Live discovery progress, separate from editable review forms."""

from math import ceil

import streamlit as st


def duration_label(seconds: float) -> str:
    minutes, seconds = divmod(max(0, ceil(seconds)), 60)
    return f"{minutes:02d}:{seconds:02d}"


def render_progress(record: dict, running: bool, *, compact: bool = False) -> None:
    status = record["status"]
    if status == "running" and not running:
        status = "interrupted — application restarted"
    if compact:
        labels = {"completed": "Scan complete", "partial": "Scan incomplete",
                  "failed": "Scan failed", "stopped": "Scan stopped"}
        label = record["phase"] if running else labels.get(status, "Scan interrupted")
        st.caption(f"{label} · {record['added']} added")
        if running:
            scan_total = record.get("scan_batches_total", 0)
            scan_completed = record.get("scan_batches_completed", 0)
            if scan_total:
                st.progress(
                    min(max(scan_completed / scan_total, 0.0), 1.0),
                    text=f"{scan_completed} / {scan_total} port batches scanned",
                )
            total = record["endpoints"]
            if total:
                st.progress(min(max(record["processed"] / total, 0.0), 1.0),
                            text=f"{record['processed']} / {total} discovered addresses checked")
            elif record.get("remaining_seconds") is not None:
                st.caption(f"Time remaining: {duration_label(record['remaining_seconds'])}")
        return
    st.write(f"{record['phase']} · {status}")
    st.caption(record["detail"])
    if record.get("skipped_active_services"):
        st.caption(f"Skipped {record['skipped_active_services']} active saved endpoint(s).")

    scan_total = record.get("scan_batches_total", 0)
    scan_completed = record.get("scan_batches_completed", 0)
    if scan_total:
        st.progress(
            min(max(scan_completed / scan_total, 0.0), 1.0),
            text=f"{scan_completed} / {scan_total} port batches scanned",
        )
        if record.get("scan_detail"):
            st.caption(record["scan_detail"])

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
