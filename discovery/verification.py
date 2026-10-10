"""Conservative verification rules, independent of the model's confidence."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from discovery.models import Observation, Proposal
from discovery.probe import Probe, origin


# Specific firmware/product markers, not a port or a generic manufacturer name.
ROUTER_MARKERS = {
    "ASUS Router": r"\bASUSWRT\b|\bASUS\s+(?:Wireless\s+)?Router\b|\bASUS\s+Login\b",
    "MikroTik RouterOS": r"\bRouterOS\b|\bMikroTik\s+WebFig\b",
    "OpenWrt LuCI": r"\bOpenWrt\b|\bLuCI\s+(?:Configuration|Administration)\b",
    "TP-Link Router": r"\bTP[- ]LINK\s+(?:Wireless\s+)?(?:Router|Archer)\b|\bArcher\s+[ACX]\w*\b",
    "NETGEAR Router": r"\bNETGEAR\s+(?:Router|Nighthawk|genie)\b",
    "Ubiquiti UniFi": r"\bUbiquiti\b|\bUniFi\s+(?:Network|Gateway|Console)\b",
    "FRITZ!Box Router": r"\bFRITZ!?Box\b",
    "Synology Router": r"\bSynology\s+Router\b|\bSynology\s+Router\s+Manager\b|\bSRM\s+Login\b",
    "GL.iNet Router": r"\bGL[.-]?iNet\b",
    "Keenetic Router": r"\bKeenetic\b",
    "Linksys Router": r"\bLinksys\s+(?:Smart\s+Wi-Fi|Router)\b",
    "D-Link Router": r"\bD-Link\s+(?:Router|Web)\b",
    "Zyxel Router": r"\bZyxel\s+(?:Router|Network)\b",
    "Huawei Router": r"\bHuawei\s+(?:Router|Home\s+Gateway)\b",
    "DrayTek Router": r"\bDrayTek\b|\bVigor\s+Router\b",
    "pfSense Router": r"\bpfSense\b",
    "OPNsense Router": r"\bOPNsense\b",
}

ROUTER_WORDS = re.compile(
    r"\b(?:router|routeros|gateway|firewall|openwrt|pfsense|opnsense|fritz!?box|webfig)\b",
    re.I,
)


def is_router_name(name: str) -> bool:
    return name in ROUTER_MARKERS or bool(ROUTER_WORDS.search(name))


def router_identity(observation: Observation) -> str:
    evidence = f"{observation.title}\n{observation.text}"
    matches = [name for name, pattern in ROUTER_MARKERS.items() if re.search(pattern, evidence, re.I)]
    if matches:
        return matches[0] if len(matches) == 1 else ""
    # Unknown vendors can still be recognized when the HTML title itself says
    # that this is a router/gateway/firewall. The title becomes the local name;
    # verification still requires a second resource and a fresh re-check.
    title = " ".join(observation.title.split()).strip(" -|·")[:60]
    return title if 2 <= len(title) <= 60 and ROUTER_WORDS.search(title) else ""


def router_names_match(a: str, b: str) -> bool:
    if not is_router_name(a) or not is_router_name(b):
        return False
    if names_match(a, b):
        return True
    # Known signatures are canonical even when a model includes a model number.
    known_a = next((name for name in ROUTER_MARKERS if words(name) in words(a) or words(a) in words(name)), "")
    known_b = next((name for name in ROUTER_MARKERS if words(name) in words(b) or words(b) in words(name)), "")
    return bool(known_a and known_a == known_b)


def panel_matches(name: str, observation: Observation) -> bool:
    if is_router_name(name):
        return router_names_match(name, router_identity(observation))
    return in_title(name, observation.title)


def words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def names_match(a: str, b: str) -> bool:
    aliases = {"proxmox": "proxmox ve", "proxmox virtual environment": "proxmox ve"}
    a, b = aliases.get(words(a), words(a)), aliases.get(words(b), words(b))
    return bool(a and b and a == b)


def in_title(name: str, title: str) -> bool:
    normalized = words(name)
    if normalized in {"proxmox", "proxmox ve", "proxmox virtual environment"}:
        return "proxmox" in words(title).split()
    return bool(normalized and f" {normalized} " in f" {words(title)} ")


def identity(observation: Observation) -> str:
    if observation.product:
        return observation.product
    path = urlsplit(observation.url).path.rstrip("/")
    facts = observation.facts
    if path == "/api2/json/version" and all(facts.get(k) for k in ("version", "release", "repoid")):
        return "Proxmox VE"
    if path == "/control/status" and isinstance(facts.get("dns_addresses"), list) and isinstance(facts.get("dns_port"), int) and isinstance(facts.get("running"), bool) and facts.get("version"):
        return "AdGuard Home"
    return ""


def redirects_to(observation: Observation, target: str) -> bool:
    redirects = observation.facts.get("redirects", [])
    return isinstance(redirects, list) and target in redirects


def assess(proposal: Proposal, observations: list[Observation]) -> tuple[bool, str]:
    # A rendered/fresh response replaces an earlier representation of that URL.
    latest = {o.url: o for o in observations}
    successful = [o for o in latest.values() if 200 <= o.status < 300 and not o.error]
    panel = next((o for o in successful if o.url == proposal.url and "html" in o.content_type and panel_matches(proposal.name, o)), None)
    if panel is None:
        return False, "The proposed panel did not return a matching application title."
    machine = [o for o in successful if "json" in o.content_type and o.url != panel.url and identity(o)]
    if any(not names_match(proposal.name, identity(o)) for o in machine):
        return False, "Page title and machine-readable product identity disagree."
    if is_router_name(proposal.name):
        assets = [o for o in successful if o.url != panel.url and o.digest != panel.digest
                  and ("javascript" in o.content_type or "css" in o.content_type or "json" in o.content_type)
                  and router_identity(o)]
        if any(not router_names_match(proposal.name, router_identity(o)) for o in assets):
            return False, "Router panel and separate firmware resource disagree."
        if any(router_names_match(proposal.name, router_identity(o)) for o in assets):
            return True, "Router panel and separate firmware resource contain matching product markers."
        redirect_sources = [o for o in latest.values() if o.url != panel.url and not o.error
                            and 200 <= o.status < 400 and redirects_to(o, panel.url)]
        if redirect_sources:
            return True, "The router address redirects to a separately fetched, matching branded login panel."
    if not any(names_match(proposal.name, identity(o)) and o.digest != panel.digest for o in machine):
        return False, "No independent machine-readable identity confirms the page title. Please check this service."
    return True, "Fresh panel and separate machine-readable identity agree. This is network fingerprint verification, not proof of software authenticity."


async def verify(proposal: Proposal, probe: Probe) -> tuple[bool, str]:
    if origin(proposal.url) != origin(probe.endpoint):
        return False, "Proposed panel is outside the observed origin."
    cited = []
    for reference in proposal.evidence:
        obs = next((o for o in probe.observations if o.id == reference.observation_id), None)
        if obs is None or reference.quote not in f"{obs.title}\n{obs.text}":
            return False, "A cited observation or exact quote does not exist."
        router_redirect = (is_router_name(proposal.name) and 200 <= obs.status < 400
                           and redirects_to(obs, proposal.url))
        if obs.error or (not 200 <= obs.status < 300 and not router_redirect):
            return False, "The proposal cites an unsuccessful request."
        cited.append(obs)
    if len({o.url for o in cited}) < 2:
        return False, "At least two distinct observed resources must support the identification."
    # Check existing observations first, including contradictions not cited by the LLM.
    matched, reason = assess(proposal, probe.observations)
    if not matched:
        return False, reason
    machine = next(o for o in reversed(probe.observations) if o.url != proposal.url and not o.error
                   and ((200 <= o.status < 300 and "json" in o.content_type
                         and names_match(proposal.name, identity(o)))
                        or (is_router_name(proposal.name) and (
                            (200 <= o.status < 300 and router_names_match(proposal.name, router_identity(o))
                             and any(t in o.content_type for t in ("javascript", "css", "json")))
                            or (200 <= o.status < 400 and redirects_to(o, proposal.url))))))
    panel = next(o for o in reversed(probe.observations) if o.url == proposal.url)
    if panel.source == "browser":
        from discovery.browser import render_page
        fresh_panel = await render_page(probe, proposal.url, lambda *_: None)
        if fresh_panel is None:
            return False, "Could not refresh the rendered router panel; manual review required."
    else:
        fresh_panel = await probe.fetch(proposal.url, fresh=True)
    fresh_identity = await probe.fetch(machine.url, fresh=True)
    return assess(proposal, [fresh_panel, fresh_identity])
