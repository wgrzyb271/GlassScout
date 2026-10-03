"""Service data, persistence, and local reachability helpers."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from dashboard.config import DATA_FILE, DEFAULT_SERVICES, ROOT_DIR, TONES

LEGACY_DATA_FILE = ROOT_DIR / "services.json"


def clean_service(item: object) -> dict[str, str]:
    raw = item if isinstance(item, dict) else {}
    tone = str(raw.get("tone", "blue"))
    return {
        "id": str(raw.get("id") or uuid4()),
        "name": str(raw.get("name") or "Untitled service")[:60],
        "kind": str(raw.get("kind") or "CUSTOM SERVICE")[:60].upper(),
        "icon": str(raw.get("icon") or "◈")[:4],
        "description": str(raw.get("description") or "A custom local service.")[:240],
        "url": str(raw.get("url") or "")[:500],
        "tone": tone if tone in TONES else "blue",
    }


def load_services() -> list[dict[str, str]]:
    source = DATA_FILE if DATA_FILE.exists() else LEGACY_DATA_FILE
    try:
        saved = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(saved, list):
            services = [clean_service(item) for item in saved]
            if source == LEGACY_DATA_FILE:
                save_services(services)
            return services
    except (OSError, json.JSONDecodeError):
        pass
    return [clean_service(item) for item in DEFAULT_SERVICES]


def save_services(services: list[dict[str, str]]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps([clean_service(item) for item in services], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def normalized_url(value: str) -> str:
    value = value.strip()
    if not value:
        return "#"
    return value if "://" in value else f"http://{value}"


def is_available(url: str) -> bool:
    """Check whether a service's TCP endpoint accepts a connection."""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    if not parsed.hostname:
        return False
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((parsed.hostname, port), timeout=0.45):
            return True
    except (OSError, ValueError):
        return False
