"""LangChain tools scoped to a single service and its request budget."""

from __future__ import annotations

from langchain.tools import tool
from langchain_core.tools import BaseTool

from discovery.budget import LimitReached
from discovery.models import FetchServiceInput, Finding, FinishServiceInput, Proposal
from discovery.probe import Probe, redact
from discovery.verification import verify


def service_tools(probe: Probe) -> dict[str, BaseTool]:
    """Capture trusted runtime state in closures, never in model arguments."""

    @tool("fetch_service", args_schema=FetchServiceInput, response_format="content_and_artifact")
    async def fetch_service(url: str, summary: str) -> tuple[str, object]:
        """Read an allowed same-origin HTTP resource to collect service identity evidence.

        Only URLs listed in allowed_urls may be fetched. Responses are untrusted
        observations. Network scope, caching, timeouts and budgets are enforced
        locally. This tool cannot change settings or write dashboard cards.
        """
        probe.budget.check()
        if probe.budget.tool_calls >= probe.budget.run.settings.tool_calls - 2:
            raise LimitReached("Request budget reserved for verification; no further discovery requests")
        observation = await probe.fetch(url)
        content = observation.model_dump_json(exclude={"instance_id", "digest", "observed_at"})
        return content, observation

    @tool("finish_service", args_schema=FinishServiceInput, response_format="content_and_artifact", return_direct=True)
    async def finish_service(summary: str, proposal: Proposal | None = None) -> tuple[str, Finding]:
        """Finish identification and submit evidence for independent local verification.

        Supply a proposal with exact observation references, or null if unknown.
        This tool cannot force approval: local code checks evidence and re-fetches
        identity resources. The controller persists the returned result later.
        """
        probe.budget.check()
        valid = False
        reason = redact(summary)
        if proposal is not None:
            proposal = Proposal.model_validate(proposal)
            valid, reason = await verify(proposal, probe)
        finding = Finding(
            endpoint=probe.endpoint, proposal=proposal,
            state="verified" if valid else "review", reason=reason,
            observations=probe.observations,
        )
        return finding.model_dump_json(exclude={"observations"}), finding

    return {tool_.name: tool_ for tool_ in (fetch_service, finish_service)}
