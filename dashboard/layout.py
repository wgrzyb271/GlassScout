"""Persistent ordering and iOS-style service groups."""

from __future__ import annotations

from pathlib import Path
import re
from uuid import uuid4

from dashboard.config import LAYOUT_FILE
from dashboard.persistence import atomic_json, data_lock


def _service_item(service_id: str) -> dict:
    return {"type": "service", "id": service_id}


def normalize_layout(value: object, service_ids: list[str]) -> dict:
    """Validate component data, retain each known service once, append new ones."""
    known = set(service_ids)
    used: set[str] = set()
    items: list[dict] = []
    raw_items = value.get("items", []) if isinstance(value, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "service":
            service_id = str(raw.get("id", ""))
            if service_id in known and service_id not in used:
                items.append(_service_item(service_id))
                used.add(service_id)
            continue
        if raw.get("type") != "group" or not isinstance(raw.get("service_ids"), list):
            continue
        members = []
        for candidate in raw["service_ids"]:
            service_id = str(candidate)
            if service_id in known and service_id not in used:
                members.append(service_id)
                used.add(service_id)
        if len(members) == 1:
            items.append(_service_item(members[0]))
        elif len(members) >= 2:
            group_id = str(raw.get("id", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", group_id):
                group_id = f"group-{uuid4()}"
            name = str(raw.get("name", "Group")).strip()[:40] or "Group"
            items.append({"type": "group", "id": group_id, "name": name, "service_ids": members})

    items.extend(_service_item(service_id) for service_id in service_ids if service_id not in used)
    return {"version": 1, "items": items}


def load_layout(service_ids: list[str], path: Path = LAYOUT_FILE) -> dict:
    try:
        import json
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return normalize_layout(raw, service_ids)


def save_layout(value: object, service_ids: list[str], path: Path = LAYOUT_FILE) -> dict:
    layout = normalize_layout(value, service_ids)
    with data_lock(path.parent):
        atomic_json(path, layout)
    return layout
