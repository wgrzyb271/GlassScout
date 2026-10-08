"""HTML components for service cards and the dashboard header."""

from __future__ import annotations

from datetime import datetime
from html import escape

import streamlit as st

from dashboard.layout import load_layout, save_layout
from dashboard.service_board import service_board, service_board_key


def _status_badge(state: bool | None) -> str:
    if state is None:
        return '<span class="status ready"><i></i>LINK READY</span>'
    if state:
        return '<span class="status online"><i></i>ONLINE</span>'
    return '<span class="status offline"><i></i>UNREACHABLE</span>'


def _service_card(service: dict[str, str | bool | None]) -> str:
    state = service.get("online")
    state = state if isinstance(state, bool) else None
    tone = escape(str(service.get("tone", "blue")), quote=True)
    url = escape(str(service.get("url", "#")), quote=True)
    return f'''<article class="liquid-glass-card {tone}">
      <div class="card-glare"></div>
      <div class="card-topline"><span class="service-icon">{escape(str(service.get("icon", "◈")))}</span>{_status_badge(state)}</div>
      <h2>{escape(str(service.get("name", "Service")))}</h2>
      <p class="service-kind">{escape(str(service.get("kind", "CUSTOM SERVICE")))}</p>
      <p class="description">{escape(str(service.get("description", "")))}</p>
      <div class="telemetry">
        <div><span>ENDPOINT</span><strong>{escape(str(service.get("endpoint", "")))}</strong></div>
        <div><span>ACCESS</span><strong>Secure link ↗</strong></div>
      </div>
      <a class="dashboard-btn" href="{url}" target="_blank" rel="noopener noreferrer">
        Open console <span>→</span>
      </a>
    </article>'''


def render_header(now: datetime, *, daytime: bool) -> None:
    theme_name = "DAY SHIFT" if daytime else "NIGHT SHIFT"
    st.markdown(
        f'''<section class="hero">
          <div><div class="eyebrow">{theme_name} · SYSTEM NOMINAL</div>
          <h1>Home Lab<br><em>Command Center.</em></h1>
          <p class="hero-copy">A quiet, high-clarity interface for the machines and services that keep your local infrastructure moving.</p></div>
          <div class="clock"><div class="clock-time">{now.strftime("%H:%M:%S")}</div><div class="clock-meta">{now.strftime("%A · %d %B %Y").upper()}</div></div>
        </section>''',
        unsafe_allow_html=True,
    )


@st.fragment
def render_service_grid(services: list[dict[str, str | bool | None]]) -> None:
    """Save board interactions without rerunning the background or sidebar."""
    service_ids = [str(service["id"]) for service in services]
    # Streamlit updates the widget's session state before either a full run or
    # a fragment run. Save it before rendering so the component receives the
    # latest layout in this same run, without requiring a scoped rerun.
    event = st.session_state.get(service_board_key(services))
    if isinstance(event, dict) and event.get("type") == "layout" and isinstance(event.get("layout"), dict):
        nonce = str(event.get("nonce", ""))
        if nonce and nonce != st.session_state.get("service_board_event"):
            save_layout(event["layout"], service_ids)
            st.session_state["service_board_event"] = nonce
    layout = load_layout(service_ids)
    service_board(services, layout)
    st.markdown(
        '<div class="footerline"><span>◈ LOCAL INFRASTRUCTURE</span>'
        '<span>LIQUID GLASS / v2.0</span></div>',
        unsafe_allow_html=True,
    )
