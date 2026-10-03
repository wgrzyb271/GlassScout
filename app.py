"""Streamlit entry point for the Home Lab Command Center."""

from datetime import datetime

import streamlit as st

from dashboard.components import render_header, render_service_grid
from dashboard.services import is_available, load_services, normalized_url
from dashboard.sidebar import render_sidebar
from dashboard.style_loader import inject_styles


st.set_page_config(
    page_title="Home Lab · Command Center",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)

daytime = 6 <= datetime.now().hour < 18
inject_styles(daytime=daytime)

services = load_services()
with st.sidebar:
    services, check_heartbeats = render_sidebar(services)

for service in services:
    endpoint = service["url"]
    service["endpoint"] = endpoint
    service["url"] = normalized_url(endpoint)
    service["online"] = is_available(service["url"]) if check_heartbeats else None

@st.fragment(run_every="1s")
def live_header() -> None:
    """Refresh only the clock/header, once per second."""
    current_time = datetime.now()
    render_header(current_time, daytime=6 <= current_time.hour < 18)


live_header()
render_service_grid(services)
