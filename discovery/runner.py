"""One background job per dashboard process; no Streamlit calls in workers."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import shutil
from threading import Event, RLock, Thread
from time import monotonic

from discovery.agent import identify
from discovery.budget import Budget, LimitReached
from discovery.models import Finding, RunRecord, Settings, now
from discovery.network import discover_mdns, scan_tcp
from discovery.probe import Probe, resolve_local
from discovery.provider import create_model, ModelUnavailable
from discovery.storage import Store


class JobManager:
    def __init__(self, data_dir: Path):
        self.store = Store(data_dir)
        self.lock = RLock()
        self.stop_event = Event()
        self.thread: Thread | None = None
        self.record: RunRecord | None = None
        self.deadline: float | None = None
        self.scan_deadline: float | None = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def snapshot(self) -> RunRecord | None:
        with self.lock:
            record = deepcopy(self.record)
            if record is not None:
                active = self.running and record.status == "running"
                current = monotonic()
                record.remaining_seconds = max(0, self.deadline - current) if active and self.deadline is not None else None
                record.scan_remaining_seconds = max(0, self.scan_deadline - current) if active and self.scan_deadline is not None else None
            return record

    def update(self, **values) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self.record, key, value)
            self.store.save_run(self.record)

    def warning(self, message: str) -> None:
        with self.lock:
            self.record.warnings.append(message)
            self.store.save_run(self.record)
        self.store.event("warning", message)

    def start(self, settings: Settings, api_key: str, model=None) -> None:
        with self.lock:
            if self.running:
                raise ValueError("A discovery run is already active.")
            if settings.networks and not shutil.which("nmap"):
                raise ValueError("Install Nmap for subnet discovery, or deselect subnets and enter a test URL.")
            if not api_key.strip() and model is None:
                raise ValueError("Enter an API key.")
            self.stop_event = Event()
            self.deadline = monotonic() + settings.run_seconds
            self.scan_deadline = None
            self.record = RunRecord(timeout_seconds=settings.run_seconds)
            self.store.save_run(self.record)
            self.thread = Thread(target=self._worker, args=(settings, api_key, model), daemon=True, name="glassscout-discovery")
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _worker(self, settings: Settings, key: str, model=None) -> None:
        budget = Budget(settings, self.stop_event)
        budget.deadline = self.deadline
        try:
            asyncio.run(self._run(settings, key, budget, model))
            budget.check()
            self.update(status="partial" if self.record.warnings else "completed", phase="Finished")
        except LimitReached as exc:
            self.update(status="stopped" if self.stop_event.is_set() else "partial", phase=str(exc))
        except ModelUnavailable as exc:
            self.warning(str(exc))
            self.update(status="partial", phase="Model unavailable — results saved")
        except Exception as exc:
            self.store.error("discovery-run", exc, {"phase": self.record.phase if self.record else "unknown"})
            self.store.event("error", f"Run failed: {type(exc).__name__}")
            self.update(status="failed", phase=f"Run failed: {type(exc).__name__}", detail="See local diagnostics; existing services were kept.")
        finally:
            self.update(finished_at=now(), model_calls=budget.model_calls, input_chars=budget.input_chars)

    async def _run(self, settings: Settings, key: str, budget: Budget, model=None) -> None:
        model = model or create_model(settings.provider, key, settings.model)
        endpoints = list(settings.seed_urls)
        seen: set[tuple[str, int]] = set()
        def found(address, port):
            if 1 <= port <= 65535 and (address, port) not in seen:
                seen.add((address, port))
        if settings.networks:
            if settings.mdns:
                self.update(phase="Listening for advertised services")
                try:
                    for address, port in await asyncio.to_thread(discover_mdns, budget):
                        found(address, port)
                except LimitReached:
                    raise
                except Exception:
                    self.warning("mDNS unavailable; continuing with TCP discovery.")
            self.update(phase="Scanning TCP ports", detail=f"{', '.join(settings.networks)} · {settings.ports}")
            # Reserve most of the run for identifying discovered services.
            scan_budget = Budget(settings, self.stop_event)
            scan_budget.deadline = min(budget.deadline, monotonic() + settings.run_seconds * 0.4)
            with self.lock:
                self.scan_deadline = scan_budget.deadline
                self.update(scan_timeout_seconds=max(0, self.scan_deadline - monotonic()))
            try:
                await asyncio.to_thread(scan_tcp, scan_budget, found, lambda message: self.update(detail=message))
            except LimitReached:
                budget.check()
                self.warning("TCP scan reached its time allocation. Coverage is partial; increase the run time or narrow the subnet to continue.")
            finally:
                with self.lock:
                    self.scan_deadline = None
        budget.check()
        # Probe both transports regardless of port; HTTPS is preferred if both work.
        for address, port in sorted(seen):
            endpoints.extend([f"https://{address}:{port}/", f"http://{address}:{port}/"])
        endpoints = list(dict.fromkeys(endpoints))
        if len(endpoints) > settings.max_endpoints:
            self.warning(f"Endpoint limit reached: processing {settings.max_endpoints} of {len(endpoints)} candidates.")
            endpoints = endpoints[:settings.max_endpoints]
        self.update(endpoints=len(endpoints), phase="Identifying services")
        successful_transports = set()
        for index, endpoint in enumerate(endpoints):
            budget.check()
            self.update(processed=index, detail=endpoint)
            try:
                url, address = await resolve_local(endpoint)
            except (ValueError, OSError, TimeoutError):
                self.store.save_finding(Finding(endpoint=endpoint, reason="Target did not resolve to a usable local IPv4 address."))
                continue
            from urllib.parse import urlsplit
            parsed = urlsplit(url)
            transport_key = (address, parsed.port or (443 if parsed.scheme == "https" else 80), parsed.path)
            if transport_key in successful_transports:
                continue
            probe = Probe(url, address, budget.service())
            try:
                finding = await identify(probe, model, self.store.event, self.store.error)
            except ModelUnavailable as exc:
                if hasattr(exc, "finding"):
                    self.store.save_finding(exc.finding)
                raise
            # Failed protocol attempts on scanned ports are diagnostic entries,
            # while an explicitly supplied URL always gets a reviewable result.
            if probe.observations and not probe.observations[0].error:
                successful_transports.add(transport_key)
            if not probe.observations or probe.observations[0].error:
                if endpoint not in settings.seed_urls:
                    self.store.event("probe", f"{endpoint}: no usable web response")
                    continue
            saved, added = self.store.save_finding(finding)
            self.store.event(saved.state, f"{saved.endpoint}: {saved.reason}")
            self.update(added=self.record.added + int(added), model_calls=budget.model_calls, input_chars=budget.input_chars)
            if budget.model_calls >= settings.total_model_calls or budget.input_chars >= settings.input_chars:
                if index + 1 < len(endpoints):
                    raise LimitReached("Run model/input budget reached; partial results saved")
        self.update(processed=len(endpoints))
