"""Agent controls and a persistent human-review queue."""

from __future__ import annotations

import importlib.util
from inspect import signature
import json
import os
from pathlib import Path
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
        value = credential_value(name) or os.getenv(name, "")
        if value:
            return value
    return ""


def credential_value(name: str) -> str:
    """Read a bounded credential supplied to this service by systemd."""
    credentials_dir = os.getenv("CREDENTIALS_DIRECTORY")
    if credentials_dir and name.isidentifier():
        path = Path(credentials_dir) / name
        try:
            if path.is_file() and path.stat().st_size <= 16_384:
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            pass
    return ""


def configured_value(name: str, default: str = "") -> str:
    """Read non-secret settings from credentials, environment or Streamlit."""
    credential = credential_value(name)
    if credential:
        return credential
    if os.getenv(name):
        return os.getenv(name, default)
    try:
        return str(st.secrets.get(name, default))
    except FileNotFoundError:
        return default


def close_results_dialog() -> None:
    st.session_state.pop("show_discovery_results", None)
    st.session_state.pop("confirm_delete_discovery", None)


def close_discovery_dialog() -> None:
    st.session_state.pop("show_discovery_dialog", None)


_DIALOG_SUPPORTS_ON_DISMISS = "on_dismiss" in signature(st.dialog).parameters
_DISCOVERY_DIALOG_OPTIONS = {"on_dismiss": close_discovery_dialog} if _DIALOG_SUPPORTS_ON_DISMISS else {}


@st.dialog("Discover services", width="large", **_DISCOVERY_DIALOG_OPTIONS)
def render_discovery_dialog() -> None:
    from discovery.models import Settings

    job = manager()
    previous_run = job.store.read().get("run") or {}
    pending = previous_run.get("pending_urls", [])
    pending_ports = previous_run.get("pending_port_ranges", [])
    previous_networks = previous_run.get("scan_networks", [])
    options = networks()
    defaults = list(dict.fromkeys(n["cidr"] for n in options if n["default"] and n["kind"] == "LAN"))
    if not defaults:
        defaults = list(dict.fromkeys(n["cidr"] for n in options if n["kind"] == "LAN"))[:1]
    labels = {n["cidr"]: f"{n['cidr']} · {n['interface']} · {n['kind']}" for n in options}

    st.markdown("#### 1. Choose where to look")
    st.caption("Enter a known local URL, scan one or more local subnets, or use both.")
    resume = st.checkbox(
        f"Resume remaining addresses ({len(pending)})", value=False,
        disabled=job.running or not pending,
        help="Continues saved addresses without repeating subnet scanning. Uses the provider and limits selected below.",
    )
    resume_ports = st.checkbox(
        f"Resume unfinished port scan ({len(pending_ports)} batches)", value=False,
        disabled=job.running or not pending_ports or not previous_networks,
        help="Continues the saved port batches. At most the interrupted batch is repeated, not the complete port range.",
    )
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
        disabled=job.running or resume_ports or not shutil.which("nmap"),
    )
    custom = st.text_input(
        "Another local network (optional)",
        placeholder="192.168.1.0/24",
        disabled=job.running or resume_ports or not shutil.which("nmap"),
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
    key = configured_key(provider)
    if key:
        st.caption(f"{pname} API key is configured on the server.")
    else:
        st.warning(f"{pname} API key is not configured on the server.")

    with st.expander("Advanced scan settings", expanded=False):
        ports = st.text_input("TCP ports", value="1-65535", disabled=job.running)
        minutes = st.number_input("Run time limit (minutes)", min_value=1, max_value=60, value=10, disabled=job.running)
        self_signed = st.checkbox("Allow self-signed HTTPS certificates", value=False, disabled=job.running)
        service_detection = st.checkbox("Identify protocols before checking web panels", value=True, disabled=job.running,
                                        help="Uses Nmap service detection. Skips confirmed non-web protocols; may increase scan time.")
        browser_available = importlib.util.find_spec("playwright") is not None
        browser_fallback = st.checkbox("Render unresolved pages with a browser", value=False,
                                       disabled=job.running or not browser_available,
                                       help="Runs JavaScript for unresolved panels. Does not sign in or submit forms.")
        if not browser_available:
            st.caption("Optional browser support: install requirements-browser.txt and Chromium (see README).")
        skip_active = st.checkbox(
            "Skip active saved endpoints", value=True, disabled=job.running,
            help="Before subnet scanning, checks each saved TCP address and skips repeated web/AI analysis for an already reachable IP and port.",
        )

    st.caption("Full network scan uses all TCP ports, waits for AI quota resets, and runs until every port batch and discovered address finishes or you stop it.")
    start_col, full_col, cancel_col = st.columns(3)
    start = start_col.button("Start discovery", type="primary", use_container_width=True, disabled=job.running)
    full_scan = full_col.button(
        "Scan entire network", use_container_width=True,
        disabled=job.running or not shutil.which("nmap"),
        help="Scans TCP ports 1–65535 on the selected networks without a scan-time limit.",
    )
    cancel = cancel_col.button("Cancel", use_container_width=True)
    if cancel:
        close_discovery_dialog()
        st.rerun(scope="app")
    if start or full_scan:
        try:
            chosen_networks = selected + ([custom.strip()] if custom.strip() else [])
            settings = Settings(
                networks=chosen_networks if full_scan else (previous_networks if resume_ports else ([] if resume else chosen_networks)),
                seed_urls=([value.strip() for value in seed.splitlines() if value.strip()] if full_scan else
                           (pending if resume else [value.strip() for value in seed.splitlines() if value.strip()])),
                provider=provider,
                model=model.strip(),
                ports="1-65535" if full_scan else ((previous_run.get("scan_ports") or ports.strip()) if resume_ports else ports.strip()),
                run_seconds=int(minutes * 60),
                allow_self_signed=self_signed,
                service_detection=service_detection,
                browser_fallback=browser_fallback,
                skip_active_services=skip_active,
                seed_urls_are_manual=full_scan or not resume,
                scan_chunks=[] if full_scan else (pending_ports if resume_ports else []),
                unlimited_run=full_scan,
                max_endpoints=256 if full_scan else 64,
                total_model_calls=300 if full_scan else 60,
                input_chars=2_000_000 if full_scan else 600_000,
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
        close_results_dialog()
        if _DIALOG_SUPPORTS_ON_DISMISS:
            st.session_state["show_discovery_dialog"] = True
            st.rerun()
        else:
            render_discovery_dialog()
    elif _DIALOG_SUPPORTS_ON_DISMISS and st.session_state.get("show_discovery_dialog"):
        render_discovery_dialog()
    elif st.session_state.get("show_discovery_results"):
        render_results_dialog()


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
    if record:
        render_progress(record, job.running, compact=True)
    pending = sum(f.get("state") == "review" for f in data["findings"].values())
    if pending:
        st.caption(f"{pending} services need review")
    if st.button("Results & details", key="open_discovery_results", use_container_width=True):
        close_discovery_dialog()
        st.session_state["show_discovery_results"] = True
        st.rerun(scope="app")
    if job.running and st.button("Stop scan", use_container_width=True):
        job.stop()


_RESULTS_DIALOG_OPTIONS = {"on_dismiss": close_results_dialog} if _DIALOG_SUPPORTS_ON_DISMISS else {}


@st.dialog("Discovery results", width="large", **_RESULTS_DIALOG_OPTIONS)
def render_results_dialog() -> None:
    from discovery.models import Finding

    job = manager()
    data = job.store.read()
    run = job.snapshot()
    record = run.model_dump() if run else data["run"]
    findings = [Finding.model_validate(v) for v in data["findings"].values()]
    pending = [f for f in findings if f.state == "review"]
    if record:
        port_batches = len(record.get("pending_port_ranges", []))
        st.caption(f"{record['added']} added · {len(pending)} need review · {len(record.get('pending_urls', []))} addresses remaining · {port_batches} port batches remaining")
        if record.get("pending_urls") and not job.running:
            st.caption("To continue, choose Resume remaining addresses in Discover services.")
        if record.get("pending_port_ranges") and not job.running:
            st.caption("To continue port coverage, choose Resume unfinished port scan in Discover services.")
    if pending:
        choices = {f.id: f for f in pending}
        selected = st.selectbox(
            "Service to review", list(choices),
            format_func=lambda key: f"{choices[key].proposal.name if choices[key].proposal else 'Unknown service'} · {choices[key].endpoint}",
        )
        finding = choices[selected]
        st.caption(finding.reason)
        with st.form(f"review_{finding.id}"):
            name = st.text_input("Service name", value=finding.proposal.name if finding.proposal else "Unknown service")
            url = st.text_input("Panel URL", value=finding.proposal.url if finding.proposal else finding.endpoint)
            st.link_button("Open observed panel", finding.proposal.url if finding.proposal else finding.endpoint)
            approve = st.form_submit_button("Approve / save changes", disabled=job.running)
            reject = st.form_submit_button("Reject", disabled=job.running)
        if approve or reject:
            try:
                job.store.review(finding.endpoint, approve=approve, name=name, url=url)
                st.rerun(scope="app")
            except ValueError as exc:
                st.error(str(exc))
    else:
        st.info("No services waiting for review.")
    download_col, delete_col = st.columns(2)
    download_col.download_button(
        "Download discovery report", data=json.dumps(data, indent=2, ensure_ascii=False),
        file_name="glassscout-discovery.json", mime="application/json", use_container_width=True,
    )
    if delete_col.button("Delete history", use_container_width=True, disabled=job.running):
        st.session_state["confirm_delete_discovery"] = True
        st.rerun(scope="app")
    if st.session_state.get("confirm_delete_discovery"):
        st.warning("Delete all discovery runs, findings and diagnostics? Service cards will be kept.")
        confirm_col, cancel_col = st.columns(2)
        if confirm_col.button("Confirm deletion", type="primary", use_container_width=True):
            job.store.clear_history()
            st.session_state.pop("agent_refresh_signature", None)
            st.session_state.pop("agent_saved_urls", None)
            close_results_dialog()
            st.rerun(scope="app")
        if cancel_col.button("Cancel deletion", use_container_width=True):
            st.session_state.pop("confirm_delete_discovery", None)
            st.rerun(scope="app")
    if st.button("Close", key="close_discovery_results"):
        close_results_dialog()
        st.rerun(scope="app")
