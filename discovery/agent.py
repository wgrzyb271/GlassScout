"""Run service identification through LangChain's create_agent runtime."""

from __future__ import annotations

from typing import Callable

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError

from discovery.budget import LimitReached
from discovery.middleware import DiscoveryGuard
from discovery.models import Finding
from discovery.probe import Probe
from discovery.provider import ModelUnavailable
from discovery.prompts import REACT_SYSTEM_PROMPT
from discovery.tools import service_tools
from discovery.enrichment import enrich
from discovery.verification import assess, verify


async def identify(
    probe: Probe,
    model: BaseChatModel,
    event: Callable[[str, str], None],
    error: Callable[[str, Exception, dict[str, str]], object] | None = None,
) -> Finding:
    tools = list(service_tools(probe).values())
    guard = DiscoveryGuard(probe, tools, event, error)
    local_proposal = None
    reason = "The agent finished without a verified service proposal."
    try:
        await probe.fetch(probe.endpoint)
        first = probe.observations[-1]
        if first.error:
            return Finding(endpoint=probe.endpoint, reason=f"Initial request failed: {first.error}", observations=probe.observations)

        local_proposal = guard.proposal = await enrich(probe, event)
        if guard.proposal is not None and assess(guard.proposal, probe.observations)[0]:
            verified, detail = await verify(guard.proposal, probe)
            if verified:
                event("fingerprint", f"Recognized {guard.proposal.name} without an AI call.")
                return Finding(endpoint=probe.endpoint, proposal=guard.proposal, state="verified",
                               reason=detail, observations=probe.observations)

        agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=REACT_SYSTEM_PROMPT,
            middleware=[guard],
            name="glassscout_discovery",
        )
        # LangChain owns model/tool routing and message history. No manual ReAct
        # loop or tool dispatch lives here. Each service gets an isolated graph.
        await agent.ainvoke(
            {"messages": [HumanMessage(content="Identify the local service using observed evidence.")]},
            config={"callbacks": [], "recursion_limit": probe.budget.run.settings.model_calls * 6 + 10},
        )
        probe.budget.check()
        if guard.finding is not None:
            if guard.finding.proposal is None and local_proposal is not None:
                guard.finding.proposal = local_proposal
                guard.finding.reason = "Product markers suggest this name, but independent verification is incomplete; please review."
            return guard.finding
    except LimitReached as exc:
        reason = str(exc)
        guard.trace("stop", reason)
    except GraphRecursionError:
        reason = "LangChain graph step limit reached; manual review required."
        guard.trace("stop", reason)
    except ModelUnavailable as exc:
        guard.trace("stop", str(exc))
        exc.finding = Finding(endpoint=probe.endpoint, proposal=guard.proposal, reason=str(exc), observations=probe.observations)
        raise
    return Finding(endpoint=probe.endpoint, proposal=guard.proposal, reason=reason, observations=probe.observations)
