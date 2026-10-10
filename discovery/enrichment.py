"""Collect useful evidence before spending model calls on generic guesses."""

from urllib.parse import urljoin, urlsplit

from discovery.models import EvidenceRef, Proposal
from discovery.verification import in_title, is_router_name, router_identity, router_names_match, identity, redirects_to


def product(observation):
    for name in ("Proxmox VE", "AdGuard Home"):
        if in_title(name, observation.title):
            return name
    return router_identity(observation)


def proposal_for(probe):
    panels = [o for o in probe.observations if 200 <= o.status < 300 and not o.error
              and "html" in o.content_type and product(o)]
    if not panels:
        return None
    panel = panels[-1]
    name = product(panel)
    supporting = [o for o in probe.observations if o.url != panel.url and not o.error
                  and ((200 <= o.status < 300
                        and (identity(o) == name or router_names_match(name, router_identity(o))))
                       or (200 <= o.status < 400 and is_router_name(name)
                           and redirects_to(o, panel.url)))]
    refs = [EvidenceRef(observation_id=o.id, quote=(o.title or o.text)[:240])
            for o in [panel, *supporting[:4]] if len(o.title or o.text) >= 2]
    return Proposal(name=name, url=panel.url, evidence=refs)


async def enrich(probe, event):
    current = probe.observations[-1]
    visited = {current.url}
    # Follow only actual same-origin HTTP/meta redirects, never guessed JS.
    for _ in range(3):
        redirects = probe.redirects.get(current.url, [])
        target = next((u for u in redirects if u not in visited), None)
        if not target or probe.budget.tool_calls >= probe.budget.run.settings.tool_calls - 4:
            break
        visited.add(target)
        current = await probe.fetch(target)
        event("probe", f"Followed observed redirect: {target} · HTTP {current.status}")
        if current.error:
            break
    if (probe.budget.run.settings.browser_fallback and "html" in current.content_type
            and 200 <= current.status < 300 and not product(current)
            and probe.budget.tool_calls < probe.budget.run.settings.tool_calls - 4):
        from discovery.browser import render_page
        event("browser", f"Rendering unresolved panel: {current.url}")
        rendered = await render_page(probe, current.url, event)
        if rendered is not None:
            current = rendered
    name = product(current)
    if name in {"Proxmox VE", "AdGuard Home"}:
        path = "/api2/json/version" if name == "Proxmox VE" else "/control/status"
        if probe.budget.tool_calls < probe.budget.run.settings.tool_calls - 3:
            await probe.fetch(urljoin(probe.endpoint, path))
    elif name:
        # Static firmware resources provide a second independent URL without
        # inventing API paths or trying authenticated settings endpoints.
        assets = [u for u in current.links if urlsplit(u).path.lower().endswith((".js", ".css", ".json"))]
        for target in assets[:2]:
            if probe.budget.tool_calls >= probe.budget.run.settings.tool_calls - 3:
                break
            await probe.fetch(target)
    return proposal_for(probe)
