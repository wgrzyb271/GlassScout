"""Conservative verification rules, independent of the model's confidence."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from discovery.models import Observation, Proposal
from discovery.probe import Probe, origin


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


def assess(proposal: Proposal, observations: list[Observation]) -> tuple[bool, str]:
    successful = [o for o in observations if 200 <= o.status < 300 and not o.error]
    panel = next((o for o in successful if o.url == proposal.url and "html" in o.content_type and in_title(proposal.name, o.title)), None)
    if panel is None:
        return False, "The proposed panel did not return a matching application title."
    machine = [o for o in successful if "json" in o.content_type and o.url != panel.url and identity(o)]
    if any(not names_match(proposal.name, identity(o)) for o in machine):
        return False, "Page title and machine-readable product identity disagree."
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
        if obs.error or not 200 <= obs.status < 300:
            return False, "The proposal cites an unsuccessful request."
        cited.append(obs)
    if len({o.url for o in cited}) < 2:
        return False, "At least two distinct observed resources must support the identification."
    # Check existing observations first, including contradictions not cited by the LLM.
    matched, reason = assess(proposal, probe.observations)
    if not matched:
        return False, reason
    machine = next(o for o in probe.observations if 200 <= o.status < 300 and not o.error and "json" in o.content_type and names_match(proposal.name, identity(o)))
    fresh_panel = await probe.fetch(proposal.url, fresh=True)
    fresh_identity = await probe.fetch(machine.url, fresh=True)
    return assess(proposal, [fresh_panel, fresh_identity])
