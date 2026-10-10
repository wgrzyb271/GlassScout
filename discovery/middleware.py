"""Safety hooks for LangChain's agent; routing/execution stays in the framework."""

from __future__ import annotations

import asyncio
import json
import re
from time import monotonic
from typing import Callable

from langchain.agents.middleware import AgentMiddleware, ModelResponse, hook_config
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.tools import BaseTool

from discovery.budget import LimitReached
from discovery.models import Finding, FinishServiceInput, Observation
from discovery.probe import Probe, observation_for_model, redact
from discovery.provider import ModelUnavailable, ProviderRateLimited


def _status_code(exc: Exception) -> int | None:
    """Read a provider HTTP status without depending on a specific SDK."""
    direct = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    for value in (direct, response_status):
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            continue
    return None


def _is_transient_provider_error(exc: Exception) -> bool:
    status = _status_code(exc)
    if status in {429, 500, 502, 503, 504}:
        return True
    message = str(exc).upper()
    return any(token in message for token in ("429", "503", "UNAVAILABLE", "RESOURCE_EXHAUSTED"))


def _is_rate_limit(exc: Exception) -> bool:
    message = str(exc).upper()
    return _status_code(exc) == 429 or "429" in message or "RATE LIMIT" in message or "RESOURCE_EXHAUSTED" in message


def _retry_after_seconds(exc: Exception, fallback: float) -> float:
    """Extract SDK/header retry guidance while keeping raw provider text out of logs."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    candidates = []
    for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        try:
            value = headers.get(name)
        except AttributeError:
            value = None
        if value is not None:
            candidates.append(str(value))
    candidates.append(str(exc))
    patterns = (
        r"try again in\s+((?:[0-9]+(?:\.[0-9]+)?\s*(?:ms|h|m|s)\s*)+)",
        r"^\s*((?:[0-9]+(?:\.[0-9]+)?\s*(?:ms|h|m|s)\s*)+)\s*$",
        r"^\s*([0-9]+(?:\.[0-9]+)?)\s*$",
    )
    for candidate in candidates:
        for pattern in patterns:
            match = re.search(pattern, candidate, re.I)
            if match:
                duration = match.group(1).strip()
                parts = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|h|m|s)", duration, re.I)
                seconds = sum(float(n) * {"ms": .001, "s": 1, "m": 60, "h": 3600}[unit.lower()]
                              for n, unit in parts) if parts else float(duration)
                # Do not retry earlier than the provider requested. The caller
                # persists the queue if this exceeds the service deadline.
                return max(0.2, seconds + 0.25)
    return fallback


class DiscoveryGuard(AgentMiddleware):
    """One middleware instance per service; never shared across agent runs."""

    def __init__(self, probe: Probe, tools: list[BaseTool], event: Callable[[str, str], None], error: Callable[[str, Exception, dict[str, str]], object] | None = None):
        super().__init__()
        self.probe, self.event, self.error = probe, event, error
        self.allowed_tools = {tool.name: tool for tool in tools}
        self.schema_chars = sum(len(json.dumps(t.args_schema.model_json_schema())) + len(t.description) for t in tools)
        self.stale = 0
        self.proposal = None
        self.finding: Finding | None = None

    def log_error(self, category: str, exc: Exception, attempt: int | None = None) -> None:
        if self.error is None:
            return
        context = {
            "endpoint": self.probe.endpoint,
            "provider": self.probe.budget.run.settings.provider,
            "model": self.probe.budget.run.settings.model,
        }
        if attempt is not None:
            context["attempt"] = str(attempt)
        try:
            self.error(category, exc, context)
        except Exception:
            # Diagnostics must never mask the original provider failure.
            pass

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
            "observations": [observation_for_model(o) for o in self.probe.observations[-6:]],
            "allowed_urls": sorted(self.probe.allowed - self.probe.cache.keys())[:24],
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
        # Keep enough room to respect a provider rate-limit reset and retry
        # without exceeding the per-service budget.
        deadline = monotonic() + min(60, self.probe.budget.remaining())
        async def cancellable_wait(seconds: float) -> None:
            until = monotonic() + seconds
            while monotonic() < until:
                self.probe.budget.check()
                await asyncio.sleep(min(0.2, max(0.01, until - monotonic())))

        async def call_with_retry():  # TRANSIENT-RETRY
            for attempt in range(3):
                try:
                    if attempt:
                        self.probe.budget.model(chars)
                    return await handler(request)
                except LimitReached:
                    raise
                except Exception as exc:
                    rate_limited = _is_rate_limit(exc)
                    self.log_error("provider-rate-limit" if rate_limited else "provider-error", exc, attempt + 1)
                    if not _is_transient_provider_error(exc):
                        raise
                    fallback = 60 if rate_limited and attempt == 2 else 2 * (attempt + 1)
                    delay = _retry_after_seconds(exc, fallback)
                    if rate_limited and attempt == 2:
                        raise ProviderRateLimited(delay) from None
                    if attempt == 2:
                        raise
                    if delay + 0.5 >= deadline - monotonic():
                        if rate_limited:
                            raise ProviderRateLimited(delay) from None
                        raise ModelUnavailable("The provider did not recover within this service's time budget.") from None
                    if rate_limited:
                        self.trace("observe", f"Provider rate limit; retrying in {delay:.1f} seconds.")
                    await cancellable_wait(delay)
        task = asyncio.create_task(call_with_retry())
        try:
            # Only a cancellation watchdog, not a model/tool execution loop.
            while not task.done():
                self.probe.budget.check()
                if monotonic() >= deadline:
                    timeout = ModelUnavailable("Model request timed out; run saved for retry.")
                    self.log_error("provider-timeout", timeout)
                    raise timeout
                await asyncio.wait({task}, timeout=0.2)
            return task.result()
        except (LimitReached, ModelUnavailable):
            raise
        except ValueError:
            # after_model routes this to a bounded repair turn.
            return ModelResponse(result=[AIMessage(content="Invalid model response; a valid tool call is required.")])
        except Exception as exc:
            if _status_code(exc) == 413 or "REQUEST TOO LARGE" in str(exc).upper():
                raise ModelUnavailable(
                    "The provider rejected an oversized request. Evidence was kept; retry with the compacted scanner or another provider."
                ) from None
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
