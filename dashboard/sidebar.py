"""Sidebar controls for endpoint editing and service management."""

from __future__ import annotations

from inspect import signature
from uuid import uuid4

import streamlit as st

from dashboard.config import CUSTOM_ICON, ICONS, TONES
from dashboard.services import clean_service, mutate_services


def close_add_service_dialog() -> None:
    st.session_state.pop("adding_service", None)
    for field in ("name", "url", "kind", "description", "icon_choice", "custom_icon", "tone"):
        st.session_state.pop(f"add_service_{field}", None)


def icon_label(value: str) -> str:
    return "Enter your own…" if value == CUSTOM_ICON else f"{value}  {ICONS[value]}"


def render_icon_picker(prefix: str) -> str:
    choice = st.selectbox(
        "Icon",
        [*ICONS, CUSTOM_ICON],
        format_func=icon_label,
        key=f"{prefix}_icon_choice",
    )
    if choice == CUSTOM_ICON:
        custom = st.text_input("Custom icon", max_chars=4, key=f"{prefix}_custom_icon", placeholder="e.g. ★")
        return custom.strip() or "◈"
    return choice


_DIALOG_SUPPORTS_ON_DISMISS = "on_dismiss" in signature(st.dialog).parameters
_ADD_DIALOG_OPTIONS = {"on_dismiss": close_add_service_dialog} if _DIALOG_SUPPORTS_ON_DISMISS else {}


@st.dialog("Add service", **_ADD_DIALOG_OPTIONS)
def render_add_service_dialog() -> None:
    """Collect a new service in the same modal pattern as service editing."""
    name = st.text_input("Name", placeholder="e.g. Grafana", key="add_service_name")
    url = st.text_input("URL", placeholder="http://192.168.1.40:3000", key="add_service_url")
    kind = st.text_input("Category", value="CUSTOM SERVICE", key="add_service_kind")
    description = st.text_area("Description", placeholder="What does this service do?", key="add_service_description")
    icon = render_icon_picker("add_service")
    tone = st.selectbox("Accent", TONES, format_func=str.title, key="add_service_tone")
    submit_col, cancel_col = st.columns(2)
    submitted = submit_col.button("Add to dashboard", key="confirm_add_service", use_container_width=True, type="primary")
    cancel = cancel_col.button("Cancel", key="cancel_add_service", use_container_width=True)
    if cancel:
        close_add_service_dialog()
        st.rerun(scope="app")
    if submitted:
        if not name.strip() or not url.strip():
            st.warning("Name and URL are required.")
        else:
            mutate_services(add=clean_service({
                "id": str(uuid4()), "name": name, "url": url, "kind": kind,
                "description": description, "icon": icon, "tone": tone,
            }))
            close_add_service_dialog()
            st.session_state["service_added"] = True
            st.rerun(scope="app")


def close_service_editor() -> None:
    st.session_state.pop("editing_service_id", None)


_EDIT_DIALOG_OPTIONS = {"on_dismiss": close_service_editor} if _DIALOG_SUPPORTS_ON_DISMISS else {}


@st.dialog("Modify service", **_EDIT_DIALOG_OPTIONS)
def render_service_editor(service: dict[str, str]) -> None:
    """Edit one service in a modal so the management list stays compact."""
    service_id = service["id"]
    st.caption(service["name"])
    fields = {
        "name": st.text_input("Name", key=f"edit_name_{service_id}", max_chars=60),
        "url": st.text_input("URL", key=f"edit_url_{service_id}", max_chars=500),
        "kind": st.text_input("Category", key=f"edit_kind_{service_id}", max_chars=60),
        "description": st.text_area("Description", key=f"edit_description_{service_id}", max_chars=240),
        "icon": render_icon_picker(f"edit_{service_id}"),
        "tone": st.selectbox("Accent", TONES, key=f"edit_tone_{service_id}", format_func=str.title),
    }
    save_col, cancel_col = st.columns(2)
    save = save_col.button("Save changes", key=f"save_service_{service_id}", use_container_width=True, type="primary")
    cancel = cancel_col.button("Cancel", key=f"cancel_service_{service_id}", use_container_width=True)
    if cancel:
        close_service_editor()
        st.rerun(scope="app")
    if save:
        try:
            mutate_services(updates={service_id: fields})
        except ValueError as exc:
            st.warning(str(exc))
        else:
            st.session_state["modified_endpoints"] = {service_id: fields["url"].strip()}
            close_service_editor()
            st.rerun(scope="app")


def close_manage_services_dialog() -> None:
    st.session_state.pop("show_manage_services", None)


_MANAGE_DIALOG_OPTIONS = {"on_dismiss": close_manage_services_dialog} if _DIALOG_SUPPORTS_ON_DISMISS else {}


def prepare_service_editor(service: dict[str, str]) -> None:
    service_id = service["id"]
    st.session_state["editing_service_id"] = service_id
    for field in ("name", "url", "kind", "description", "tone"):
        st.session_state[f"edit_{field}_{service_id}"] = service[field]
    known_icon = service["icon"] in ICONS
    st.session_state[f"edit_{service_id}_icon_choice"] = service["icon"] if known_icon else CUSTOM_ICON
    st.session_state[f"edit_{service_id}_custom_icon"] = "" if known_icon else service["icon"]


@st.dialog("Manage services", width="large", **_MANAGE_DIALOG_OPTIONS)
def render_manage_services_dialog(services: list[dict[str, str]]) -> None:
    """List service actions in a dedicated modal."""
    list_container = st.container(height=480, border=False) if len(services) > 6 else st.container(border=False)
    with list_container:
        if not services:
            st.info("No services to manage yet.")
        for service in services:
            name_col, modify_col, remove_col = st.columns([2, 1, 1])
            name_col.write(service["name"])
            if modify_col.button("Modify", key=f"modify_{service['id']}", use_container_width=True):
                close_manage_services_dialog()
                prepare_service_editor(service)
                st.rerun(scope="app")
            if remove_col.button("Remove", key=f"remove_{service['id']}", use_container_width=True):
                mutate_services(remove=service["id"])
                st.rerun(scope="app")
            st.divider()
    if st.button("Close", key="close_manage_services", use_container_width=True):
        close_manage_services_dialog()
        st.rerun(scope="app")


def render_sidebar(services: list[dict[str, str]]) -> tuple[list[dict[str, str]], bool]:
    # Apply editor changes before the endpoint widgets are instantiated.
    for service_id, url in st.session_state.pop("modified_endpoints", {}).items():
        st.session_state[f"endpoint_{service_id}"] = url
    saved_services = [dict(service) for service in services]
    if st.session_state.pop("service_added", False):
        st.toast("Service added", icon="✅")
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

    if st.button("Add service", key="open_add_service", use_container_width=True):
        if _DIALOG_SUPPORTS_ON_DISMISS:
            st.session_state["adding_service"] = True
            st.rerun()
        else:
            render_add_service_dialog()
    elif _DIALOG_SUPPORTS_ON_DISMISS and st.session_state.get("adding_service"):
        render_add_service_dialog()

    if st.button("Manage services", key="open_manage_services", use_container_width=True):
        if _DIALOG_SUPPORTS_ON_DISMISS:
            st.session_state["show_manage_services"] = True
            st.rerun()
        else:
            render_manage_services_dialog(saved_services)
    elif _DIALOG_SUPPORTS_ON_DISMISS and st.session_state.get("show_manage_services"):
        render_manage_services_dialog(saved_services)

    selected = next((service for service in saved_services if service["id"] == st.session_state.get("editing_service_id")), None)
    if selected:
        render_service_editor(selected)

    return services, check_heartbeats
