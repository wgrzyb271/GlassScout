"""Agent controls and a persistent human-review queue."""

from __future__ import annotations

import importlib.util
from inspect import signature
import json
import os
import shutil

import streamlit as st

from dashboard.config import DATA_FILE
from dashboard.discovery_progress import render_progress


def dependencies_available() -> bool:
    return all(importlib.util.find_spec(name) for name in ("pydantic", "psutil", "langchain", "langchain_google_genai", "langchain_groq", "langgraph"))


@st.cache_resource
def manager():
    from discovery.runner import JobManager
    return JobManager(DATA_FILE.parent)


@st.cache_data(ttl=30)
def networks():
    from discovery.network import detect_networks
    return [n.model_dump() for n in detect_networks()]


def configured_key(provider: str = "gemini") -> str:
    names = ("GROQ_API_KEY",) if provider == "groq" else ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    for name in names:
        value = configured_value(name)
        if value:
            return value
    return ""


def configured_value(name: str, default: str = "") -> str:
    """Read agent settings from the environment or Streamlit config file."""
    if os.getenv(name):
        return os.getenv(name, default)
    try:
        return str(st.secrets.get(name, default))
    except FileNotFoundError:
        return default


def close_discovery_dialog() -> None:
    st.session_state.pop("show_discovery_dialog", None)


_DIALOG_SUPPORTS_ON_DISMISS = "on_dismiss" in signature(st.dialog).parameters
_DISCOVERY_DIALOG_OPTIONS = {"on_dismiss": close_discovery_dialog} if _DIALOG_SUPPORTS_ON_DISMISS else {}


@st.dialog("Discover services", width="large", **_DISCOVERY_DIALOG_OPTIONS)
def render_discovery_dialog() -> None:
    from discovery.models import Settings

    job = manager()
    options = networks()
    defaults = list(dict.fromkeys(n["cidr"] for n in options if n["default"] and n["kind"] == "LAN"))
    if not defaults:
        defaults = list(dict.fromkeys(n["cidr"] for n in options if n["kind"] == "LAN"))[:1]
    labels = {n["cidr"]: f"{n['cidr']} · {n['interface']} · {n['kind']}" for n in options}

    st.markdown("#### 1. Choose where to look")
    st.caption("Enter a known local URL, scan one or more local subnets, or use both.")
    seed = st.text_area(
        "Known service URLs",
        placeholder="http://192.168.1.40:3000\nhttp://127.0.0.1:8765",
        disabled=job.running,
        help="One HTTP or HTTPS URL per line. This is the fastest option when you already know an address.",
    )
    if not shutil.which("nmap"):
        st.info("Automatic subnet scanning needs Nmap. Known URLs above still work without it.")
        defaults = []
    selected = st.multiselect(
        "Detected local networks",
        list(labels),
        default=defaults,
        format_func=lambda value: labels[value],
        disabled=job.running or not shutil.which("nmap"),
    )
    custom = st.text_input(
        "Another local network (optional)",
        placeholder="192.168.1.0/24",
        disabled=job.running or not shutil.which("nmap"),
        help="Use CIDR notation. A smaller range makes discovery faster.",
    )
    if not options:
        st.caption("No local IPv4 network was detected automatically. You can still enter a known URL above.")

    st.divider()
    st.markdown("#### 2. AI identification")
    st.caption("The selected model receives sanitized service observations and proposes names and dashboard URLs.")
    provider_names = {"gemini": "Gemini", "groq": "Groq"}
    default_provider = configured_value("LLM_PROVIDER") or ("groq" if configured_key("groq") and not configured_key("gemini") else "gemini")
    provider_col, model_col = st.columns(2)
    provider = provider_col.selectbox(
        "Provider",
        list(provider_names),
        index=list(provider_names).index(default_provider) if default_provider in provider_names else 0,
        format_func=provider_names.get,
        disabled=job.running,
    )
    pname = provider_names[provider]
    default_model = configured_value("GROQ_MODEL", "openai/gpt-oss-120b") if provider == "groq" else configured_value("GEMINI_MODEL", "gemini-3.6-flash")
    model = model_col.text_input(
        "Model",
        value=default_model,
        key=f"agent_model_{provider}",
        disabled=job.running,
        help="Loaded from .streamlit/secrets.toml and editable for this session.",
    )
    key = st.text_input(
        f"{pname} API key",
        type="password",
        value=configured_key(provider),
        key=f"agent_key_{provider}",
        disabled=job.running,
        help=f"Loaded from .streamlit/secrets.toml or {'GROQ_API_KEY' if provider == 'groq' else 'GEMINI_API_KEY'}. It is never written to discovery reports.",
    )

    with st.expander("Advanced scan settings", expanded=False):
        ports = st.text_input("TCP ports", value="1-65535", disabled=job.running)
        minutes = st.number_input("Run time limit (minutes)", min_value=1, max_value=60, value=10, disabled=job.running)
        self_signed = st.checkbox("Allow self-signed HTTPS certificates", value=False, disabled=job.running)

    start_col, cancel_col = st.columns(2)
    start = start_col.button("Start discovery", type="primary", use_container_width=True, disabled=job.running)
    cancel = cancel_col.button("Cancel", use_container_width=True)
    if cancel:
        close_discovery_dialog()
        st.rerun(scope="app")
    if start:
        try:
            settings = Settings(
                networks=selected + ([custom.strip()] if custom.strip() else []),
                seed_urls=[value.strip() for value in seed.splitlines() if value.strip()],
                provider=provider,
                model=model.strip(),
                ports=ports.strip(),
                run_seconds=int(minutes * 60),
                allow_self_signed=self_signed,
            )
            job.start(settings, key)
            close_discovery_dialog()
            st.rerun(scope="app")
        except ValueError as exc:
            st.error(str(exc))
    st.caption("Discovery only connects to local IPv4 services. Progress can be partial when a time or request limit is reached.")


def render_agent_controls() -> None:
    if not dependencies_available():
        st.info("Install the updated requirements to enable service discovery.")
        st.code("python -m pip install -r requirements.txt", language="bash")
        return
    job = manager()
    if st.button("Discover services", key="open_discovery", use_container_width=True, disabled=job.running):
        if _DIALOG_SUPPORTS_ON_DISMISS:
            st.session_state["show_discovery_dialog"] = True
            st.rerun()
        else:
            render_discovery_dialog()
    elif _DIALOG_SUPPORTS_ON_DISMISS and st.session_state.get("show_discovery_dialog"):
        render_discovery_dialog()


@st.fragment(run_every="1s")
def render_agent_results() -> None:
    if not dependencies_available():
        return
    from discovery.models import Finding
    job = manager()
    try:
        data = job.store.read()
    except (OSError, ValueError):
        st.error("The discovery history could not be read. Existing services are unaffected.")
        return
    if not data["run"] and not data["findings"]:
        return
    # Updating the full app refreshes cards and controls when the worker changes
    # persisted services or finishes; fragments alone would leave cards stale.
    signature = (DATA_FILE.stat().st_mtime_ns if DATA_FILE.exists() else 0, job.running)
    previous = st.session_state.get("agent_refresh_signature")
    st.session_state["agent_refresh_signature"] = signature
    saved_urls = {}
    if DATA_FILE.exists():
        try:
            saved_urls = {s["id"]: s["url"] for s in json.loads(DATA_FILE.read_text(encoding="utf-8"))}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    previous_urls = st.session_state.get("agent_saved_urls", saved_urls)
    st.session_state["agent_saved_urls"] = saved_urls
    if previous is not None and previous != signature:
        for service_id, value in saved_urls.items():
            widget = f"endpoint_{service_id}"
            if previous_urls.get(service_id) != value and st.session_state.get(widget) == previous_urls.get(service_id) and widget in st.session_state:
                del st.session_state[widget]
        st.rerun(scope="app")
    run = job.snapshot()
    record = run.model_dump() if run else data["run"]
    # Remains visible even when the detailed review expander is collapsed.
    if record:
        render_progress(record, job.running)
        if job.running and st.button("Stop agent", use_container_width=True):
            job.stop()
            st.info("Stopping after the current bounded request…")
    with st.expander("Discovery activity & review", expanded=job.running):
        if record:
            st.caption(f"Model calls: {record['model_calls']} · input characters: {record['input_chars']}")
            for message in record["warnings"]:
                st.warning(message)
        findings = [Finding.model_validate(v) for v in data["findings"].values()]
        pending = [f for f in findings if f.state == "review"]
        st.write(f"Needs review: {len(pending)}")
        for finding in pending:
            st.divider()
            st.caption(finding.reason)
            with st.form(f"review_{finding.id}"):
                name = st.text_input("Service name", value=finding.proposal.name if finding.proposal else "Unknown service")
                url = st.text_input("Panel URL", value=finding.proposal.url if finding.proposal else finding.endpoint)
                st.link_button("Open observed panel", finding.endpoint)
                approve = st.form_submit_button("Approve / save changes", disabled=job.running)
                reject = st.form_submit_button("Reject", disabled=job.running)
            if approve or reject:
                try:
                    job.store.review(finding.endpoint, approve=approve, name=name, url=url)
                    st.rerun(scope="app")
                except ValueError as exc:
                    st.error(str(exc))
            for obs in finding.observations:
                st.caption(f"HTTP {obs.status} · {obs.url} · {obs.observed_at}")
                st.text((obs.title + "\n" + obs.text + "\n" + obs.error)[:700])
        if findings:
            st.download_button("Download discovery report", data=json.dumps(data, indent=2, ensure_ascii=False), file_name="glassscout-discovery.json", mime="application/json")
        react_events = [event for event in data["events"] if event["kind"].startswith("react_")]
        if react_events:
            st.write("ReAct · Reason → Act → Observe")
            st.caption("Short action explanations and actual tool results — not the model's private reasoning. Latest 3 events.")
            labels = {"react_reason": "Reason", "react_act": "Act", "react_observe": "Observe", "react_finish": "Finish", "react_rejected": "Rejected", "react_stop": "Stopped"}
            for event in react_events[-3:]:
                st.caption(f"{event['time']} · {labels.get(event['kind'], event['kind'])}")
                st.text(event["message"])
        for event in [event for event in data["events"] if not event["kind"].startswith("react_")][-3:]:
            st.caption(f"{event['kind']}: {event['message']}")
        if st.button("Delete discovery history", use_container_width=True, disabled=job.running):
            job.store.clear_history()
            st.session_state.pop("agent_refresh_signature", None)
            st.session_state.pop("agent_saved_urls", None)
            st.rerun(scope="app")
