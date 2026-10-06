"""Safety hooks for LangChain's agent; routing/execution stays in the framework."""

from __future__ import annotations

import asyncio
import json
from time import monotonic
from typing import Callable

from langchain.agents.middleware import AgentMiddleware, ModelResponse, hook_config
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.tools import BaseTool

from discovery.budget import LimitReached
from discovery.models import Finding, FinishServiceInput, Observation
from discovery.probe import Probe, redact
from discovery.provider import ModelUnavailable


class DiscoveryGuard(AgentMiddleware):
    """One middleware instance per service; never shared across agent runs."""

    def __init__(self, probe: Probe, tools: list[BaseTool], event: Callable[[str, str], None]):
        super().__init__()
        self.probe, self.event = probe, event
        self.allowed_tools = {tool.name: tool for tool in tools}
        self.schema_chars = sum(len(json.dumps(t.args_schema.model_json_schema())) + len(t.description) for t in tools)
        self.stale = 0
        self.proposal = None
        self.finding: Finding | None = None

    def trace(self, phase: str, message: str) -> None:
        """Log action summaries and real outcomes, never raw model reasoning."""
        step = self.probe.budget.model_calls
        self.event(f"react_{phase}", redact(f"Step {step} · {self.probe.endpoint} · {message}")[:600])

    async def abefore_model(self, state, runtime):
        self.probe.budget.check()
        if self.stale >= 2:
            raise LimitReached("No new evidence in two consecutive steps")
        context = {
            "endpoint": self.probe.endpoint,
            "observations": [o.model_dump(exclude={"instance_id", "digest", "observed_at"}) for o in self.probe.observations[-10:]],
            "allowed_urls": sorted(self.probe.allowed - self.probe.cache.keys()),
            "remaining_model_calls": self.probe.budget.run.settings.model_calls - self.probe.budget.model_calls,
            "remaining_fetches": max(0, self.probe.budget.run.settings.tool_calls - self.probe.budget.tool_calls - 2),
        }
        return {"messages": [HumanMessage(content=json.dumps(context, ensure_ascii=False))]}

    async def awrap_model_call(self, request, handler):
        messages = [*([request.system_message] if request.system_message else []), *request.messages]
        chars = self.schema_chars + sum(len(json.dumps(m.content, ensure_ascii=False)) + len(json.dumps(getattr(m, "tool_calls", []))) for m in messages)
        self.probe.budget.model(chars)
        google_only = {"automatic_function_calling": {"disable": True}} if self.probe.budget.run.settings.provider == "gemini" else {}
        request = request.override(
            tool_choice="any",
            # LangChain executes tools; Google's separate AFC loop must not.
            model_settings={**request.model_settings, **google_only},
        )
        deadline = monotonic() + min(25, self.probe.budget.remaining())
        async def call_with_retry():  # TRANSIENT-RETRY
            for attempt in range(3):
                try:
                    return await handler(request)
                except Exception as exc:
                    transient = any(t in str(exc) for t in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"))
                    if not transient or attempt == 2:
                        raise
                    await asyncio.sleep(2 * (attempt + 1))
        task = asyncio.create_task(call_with_retry())
        try:
            # Only a cancellation watchdog, not a model/tool execution loop.
            while not task.done():
                self.probe.budget.check()
                if monotonic() >= deadline:
                    raise ModelUnavailable("Model request timed out; run saved for retry.")
                await asyncio.wait({task}, timeout=0.2)
            return task.result()
        except (LimitReached, ModelUnavailable):
            raise
        except ValueError:
            # after_model routes this to a bounded repair turn.
            return ModelResponse(result=[AIMessage(content="Invalid model response; a valid tool call is required.")])
        except Exception:
            import os, sys; _e = sys.exc_info()[1]; print('GEMINI DEBUG', type(_e).__name__, str(_e).replace(os.getenv('GEMINI_API_KEY') or '\0', '***')[:800], flush=True)  # TEMP-DEBUG
            # Provider exceptions may contain the key; never persist their text.
            raise ModelUnavailable("The model provider is unavailable. Check the API key, model access, quota and internet connection.") from None
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        response = state["messages"][-1]
        try:
            if not isinstance(response, AIMessage) or response.invalid_tool_calls or len(response.tool_calls) != 1:
                raise ValueError("Expected exactly one native tool call")
            call = response.tool_calls[0]
            selected = self.allowed_tools.get(call["name"])
            if selected is None or not call.get("id"):
                raise ValueError("Unknown tool or missing call ID")
            selected.args_schema.model_validate(call["args"])
        except ValueError:
            self.stale += 1
            self.trace("rejected", "Invalid tool call or arguments; no action was executed.")
            # Reject the whole batch before LangChain's parallel tool node.
            # Remove malformed calls so no unmatched IDs reach the provider.
            return {
                "messages": [RemoveMessage(id=response.id), HumanMessage(content="No tools were executed. Call exactly one of fetch_service or finish_service with valid schema arguments.")],
                "jump_to": "model",
            }
        return None

    async def awrap_tool_call(self, request, handler):
        self.probe.budget.check()
        call = request.tool_call
        try:
            arguments = self.allowed_tools[call["name"]].args_schema.model_validate(call["args"])
            self.trace("reason", arguments.summary)
            target = getattr(arguments, "url", self.probe.endpoint)
            self.trace("act", f"{call['name']} → {target}")
            if isinstance(arguments, FinishServiceInput):
                self.proposal = arguments.proposal
            before = len(self.probe.cache)
            result = await handler(request)
            if isinstance(result, ToolMessage) and isinstance(result.artifact, Finding):
                self.finding = result.artifact
                self.trace("observe", f"Local verification: {self.finding.state} · {self.finding.reason}")
                self.trace("finish", f"{self.finding.state} · {self.finding.proposal.name if self.finding.proposal else 'Unknown service'}")
            elif isinstance(result, ToolMessage) and isinstance(result.artifact, Observation):
                observation = result.artifact
                self.stale = self.stale + 1 if len(self.probe.cache) == before or observation.error else 0
                self.trace("observe", f"{observation.id} · HTTP {observation.status} · {observation.error or observation.title or observation.product or 'Response received'}")
            else:
                self.stale += 1
                self.trace("observe", "Tool did not return usable evidence.")
            return result
        except LimitReached as exc:
            self.trace("observe", f"Action interrupted: {exc}")
            raise
        except ValueError:
            self.stale += 1
            self.trace("observe", "Arguments or URL rejected; no identifying evidence collected.")
            return ToolMessage(content="Arguments or URL rejected. Choose an exact allowed URL or finish.", tool_call_id=call["id"], name=call["name"], status="error")
