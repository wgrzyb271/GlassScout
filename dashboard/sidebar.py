"""Sidebar controls for endpoint editing and service management."""

from __future__ import annotations

from uuid import uuid4

import streamlit as st

from dashboard.config import TONES
from dashboard.services import clean_service, mutate_services


def render_sidebar(services: list[dict[str, str]]) -> tuple[list[dict[str, str]], bool]:
    # Apply editor changes before the endpoint widgets are instantiated.
    for service_id, url in st.session_state.pop("modified_endpoints", {}).items():
        st.session_state[f"endpoint_{service_id}"] = url
    saved_services = [dict(service) for service in services]
    st.markdown("<div class='side-brand'><span>◈</span> HOME LAB</div>", unsafe_allow_html=True)
    st.caption("NODE ROUTING / LOCAL NETWORK")
    st.markdown("<div class='sidebar-rule'></div>", unsafe_allow_html=True)
    st.markdown("<p class='sidebar-label'>SERVICE ENDPOINTS</p>", unsafe_allow_html=True)

    for service in services:
        endpoint_key = f"endpoint_{service['id']}"
        if endpoint_key not in st.session_state:
            st.session_state[endpoint_key] = service["url"]
        service["url"] = st.text_input(service["name"], key=endpoint_key)
    if st.button("Save endpoint changes", use_container_width=True):
        mutate_services(endpoints={s["id"]: s["url"] for s in services})
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
                mutate_services(add=clean_service({
                    "id": str(uuid4()), "name": name, "url": url, "kind": kind,
                    "description": description, "icon": icon, "tone": tone,
                }))
                st.rerun()

    with st.expander("Manage services", expanded=False):
        if not services:
            st.caption("No services to manage.")
        for service in saved_services:
            st.caption(service["name"])
            modify_col, remove_col = st.columns(2)
            if modify_col.button("Modify", key=f"modify_{service['id']}", use_container_width=True):
                st.session_state["editing_service_id"] = service["id"]
                for field in ("name", "url", "kind", "description", "icon", "tone"):
                    st.session_state[f"edit_{field}_{service['id']}"] = service[field]
                st.rerun()
            if remove_col.button("Remove", key=f"remove_{service['id']}", use_container_width=True):
                mutate_services(remove=service["id"])
                if st.session_state.get("editing_service_id") == service["id"]:
                    st.session_state.pop("editing_service_id", None)
                st.rerun()

        selected = next((s for s in saved_services if s["id"] == st.session_state.get("editing_service_id")), None)
        if selected:
            service_id = selected["id"]
            st.divider()
            st.caption(f"Modify · {selected['name']}")
            with st.form(f"edit_service_{service_id}"):
                fields = {
                    "name": st.text_input("Name", key=f"edit_name_{service_id}", max_chars=60),
                    "url": st.text_input("URL", key=f"edit_url_{service_id}", max_chars=500),
                    "kind": st.text_input("Category", key=f"edit_kind_{service_id}", max_chars=60),
                    "description": st.text_area("Description", key=f"edit_description_{service_id}", max_chars=240),
                    "icon": st.text_input("Icon", key=f"edit_icon_{service_id}", max_chars=4),
                    "tone": st.selectbox("Accent", TONES, key=f"edit_tone_{service_id}", format_func=str.title),
                }
                save = st.form_submit_button("Save changes", use_container_width=True)
                cancel = st.form_submit_button("Cancel", use_container_width=True)
            if cancel:
                st.session_state.pop("editing_service_id", None)
                st.rerun()
            if save:
                try:
                    mutate_services(updates={service_id: fields})
                except ValueError as exc:
                    st.warning(str(exc))
                else:
                    st.session_state["modified_endpoints"] = {service_id: fields["url"].strip()}
                    st.session_state.pop("editing_service_id", None)
                    st.rerun()

    return services, check_heartbeats
