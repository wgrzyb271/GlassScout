"""Python bridge for the draggable service-board web component."""

from __future__ import annotations

from pathlib import Path

from streamlit.components.v1 import declare_component


_COMPONENT_DIR = Path(__file__).with_name("service_board")
_service_board = declare_component("service_board", path=_COMPONENT_DIR)


def service_board(services: list[dict], layout: dict):
    return _service_board(services=services, layout=layout, default=None, key="service-board")
