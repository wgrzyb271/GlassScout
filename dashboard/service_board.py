"""Python bridge for the draggable service-board web component."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from streamlit.components.v1 import declare_component


_COMPONENT_DIR = Path(__file__).with_name("service_board")
_service_board = declare_component("service_board", path=_COMPONENT_DIR)


def service_board_key(services: list[dict]) -> str:
    # Adding/removing a service must start a fresh iframe. Reusing the old
    # component key can preserve stale frontend state until a full page reload.
    service_ids = sorted(str(service.get("id", "")) for service in services)
    membership = sha256("\0".join(service_ids).encode("utf-8")).hexdigest()[:12]
    return f"service-board-{membership}"


def service_board(services: list[dict], layout: dict):
    return _service_board(
        services=services,
        layout=layout,
        default=None,
        key=service_board_key(services),
    )
