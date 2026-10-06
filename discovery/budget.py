"""Limits are enforced outside the LLM, including nested requests and retries."""

from __future__ import annotations

from threading import Event
from time import monotonic

from discovery.models import Settings


class LimitReached(Exception):
    pass


class Budget:
    def __init__(self, settings: Settings, stop: Event):
        self.settings, self.stop = settings, stop
        self.deadline = monotonic() + settings.run_seconds
        self.model_calls = 0
        self.input_chars = 0

    def check(self) -> None:
        if self.stop.is_set():
            raise LimitReached("Stopped by user")
        if monotonic() >= self.deadline:
            raise LimitReached("Run time limit reached")

    def service(self) -> ServiceBudget:
        return ServiceBudget(self)


class ServiceBudget:
    def __init__(self, run: Budget):
        self.run = run
        self.deadline = min(run.deadline, monotonic() + run.settings.service_seconds)
        self.model_calls = 0
        self.tool_calls = 0

    def check(self) -> None:
        self.run.check()
        if monotonic() >= self.deadline:
            raise LimitReached("Service time limit reached")

    def remaining(self) -> float:
        self.check()
        return max(0.01, self.deadline - monotonic())

    def model(self, chars: int) -> None:
        self.check()
        if self.model_calls >= self.run.settings.model_calls:
            raise LimitReached("Service model call limit reached")
        if self.run.model_calls >= self.run.settings.total_model_calls:
            raise LimitReached("Run model call limit reached")
        if self.run.input_chars + chars > self.run.settings.input_chars:
            raise LimitReached("Run input budget reached")
        self.model_calls += 1
        self.run.model_calls += 1
        self.run.input_chars += chars

    def tool(self) -> None:
        self.check()
        if self.tool_calls >= self.run.settings.tool_calls:
            raise LimitReached("Service request limit reached")
        self.tool_calls += 1
