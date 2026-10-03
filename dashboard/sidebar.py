"""Sidebar controls for endpoint editing and service management."""

from __future__ import annotations

from uuid import uuid4

import streamlit as st

from dashboard.config import TONES
from dashboard.services import clean_service, save_services


def render_sidebar(services: list[dict[str, str]]) -> tuple[list[dict[str, str]], bool]:
    st.markdown("<div class='side-brand'><span>◈</span> HOME LAB</div>", unsafe_allow_html=True)
    st.caption("NODE ROUTING / LOCAL NETWORK")
    st.markdown("<div class='sidebar-rule'></div>", unsafe_allow_html=True)
    st.markdown("<p class='sidebar-label'>SERVICE ENDPOINTS</p>", unsafe_allow_html=True)

    for service in services:
        service["url"] = st.text_input(
            service["name"], value=service["url"], key=f"endpoint_{service['id']}"
        )
    if st.button("Save endpoint changes", use_container_width=True):
        save_services(services)
        st.success("Endpoints saved")

    st.markdown("<div class='sidebar-rule'></div>", unsafe_allow_html=True)
    check_heartbeats = st.toggle("Check node heartbeat", value=False)
    st.caption("A brief TCP connection checks each address when enabled.")
    st.markdown("<div class='sidebar-rule'></div>", unsafe_allow_html=True)

    with st.expander("Add service", expanded=False):
        with st.form("add-service", clear_on_submit=True):
            name = st.text_input("Name", placeholder="e.g. Grafana")
            url = st.text_input("URL", placeholder="http://192.168.1.40:3000")
            kind = st.text_input("Category", value="CUSTOM SERVICE")
            description = st.text_area("Description", placeholder="What does this service do?")
            icon = st.text_input("Icon", value="◈", max_chars=4)
            tone = st.selectbox("Accent", TONES, format_func=str.title)
            submitted = st.form_submit_button("Add to dashboard", use_container_width=True)
        if submitted:
            if not name.strip() or not url.strip():
                st.warning("Name and URL are required.")
            else:
                services.append(clean_service({
                    "id": str(uuid4()), "name": name, "url": url, "kind": kind,
                    "description": description, "icon": icon, "tone": tone,
                }))
                save_services(services)
                st.rerun()

    with st.expander("Manage services", expanded=False):
        if not services:
            st.caption("No services to manage.")
        for service in services:
            name_col, remove_col = st.columns([3, 1])
            name_col.caption(service["name"])
            if remove_col.button("Remove", key=f"remove_{service['id']}"):
                save_services([item for item in services if item["id"] != service["id"]])
                st.rerun()

    return services, check_heartbeats
