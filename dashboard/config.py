"""Project paths and built-in service definitions."""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT_DIR / "data" / "services.json"
LAYOUT_FILE = ROOT_DIR / "data" / "layout.json"
DAY_BACKGROUND_VIDEO = ROOT_DIR / "assets" / "day.mp4"
NIGHT_BACKGROUND_VIDEO = ROOT_DIR / "assets" / "night.mp4"
SAFARI_BACKGROUND_IMAGE = ROOT_DIR / "assets" / "GoldenGate_Day_optimized.jpg"
STYLE_FILE = Path(__file__).with_name("styles") / "dashboard.css"
TONES = ("amber", "violet", "blue", "mint")
ICONS = {
    "◈": "Generic",
    "◌": "Network",
    "◇": "Server",
    "⊞": "Desktop",
    "◒": "Security",
    "▦": "Dashboard",
    "◉": "Monitoring",
    "⌂": "Home",
    "⚙": "Settings",
    "☁": "Cloud",
    "⬡": "Container",
    "◆": "Storage",
}
CUSTOM_ICON = "__custom__"

DEFAULT_SERVICES = [
    {
        "id": "adguard", "name": "AdGuard Home", "kind": "DNS / PRIVACY LAYER", "icon": "◌",
        "description": "Network-wide filtering, protected DNS resolution, and transparent traffic policy.",
        "url": "http://192.168.1.5:3000", "tone": "amber",
    },
    {
        "id": "proxmox", "name": "Proxmox VE", "kind": "VIRTUALIZATION FABRIC", "icon": "◇",
        "description": "A high-density control plane for virtual machines, containers, storage, and cluster tasks.",
        "url": "https://192.168.1.10:8006", "tone": "violet",
    },
    {
        "id": "windows", "name": "Windows VM", "kind": "DESKTOP ENVIRONMENT", "icon": "⊞",
        "description": "A dedicated Windows workspace isolated inside the local compute environment.",
        "url": "http://192.168.1.20:8000", "tone": "blue",
    },
    {
        "id": "kali", "name": "Kali Linux", "kind": "SECURITY WORKBENCH", "icon": "◒",
        "description": "A focused security workspace for controlled diagnostics, testing, and network analysis.",
        "url": "http://192.168.1.30:6080", "tone": "mint",
    },
]
