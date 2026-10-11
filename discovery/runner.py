"""One background job per dashboard process; no Streamlit calls in workers."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import shutil
import socket
from threading import Event, RLock, Thread
from time import monotonic
from urllib.parse import urlsplit

from discovery.agent import identify
from discovery.budget import Budget, LimitReached
from discovery.models import Finding, RunRecord, Settings, now
from discovery.network import chunk_ports, discover_mdns, scan_tcp
from discovery.probe import Probe, resolve_local
from discovery.provider import create_model, ModelUnavailable, ProviderRateLimited
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

    def start(self, settings: Settings, api_key: str, model=None, *, fallback_api_key: str = "") -> None:
        with self.lock:
            if self.running:
                raise ValueError("A discovery run is already active.")
            if settings.networks and not shutil.which("nmap"):
                raise ValueError("Install Nmap for subnet discovery, or deselect subnets and enter a test URL.")
            if not api_key.strip() and not fallback_api_key.strip() and model is None:
                raise ValueError("Enter an API key.")
            self.stop_event = Event()
            self.deadline = None if settings.unlimited_run else monotonic() + settings.run_seconds
            self.scan_deadline = None
            previous_run = self.store.read().get("run") or {}
            preserve_scan_checkpoint = not settings.networks and not settings.seed_urls_are_manual
            resuming_scan = bool(settings.scan_chunks and previous_run.get("pending_port_ranges"))
            self.record = RunRecord(
                timeout_seconds=0 if settings.unlimited_run else settings.run_seconds,
                unlimited_run=settings.unlimited_run,
                pending_urls=settings.seed_urls,
                scan_networks=settings.networks or (previous_run.get("scan_networks", []) if preserve_scan_checkpoint else []),
                scan_ports=(settings.ports if settings.networks else previous_run.get("scan_ports", "")) if preserve_scan_checkpoint or settings.networks else "",
                pending_port_ranges=settings.scan_chunks or (previous_run.get("pending_port_ranges", []) if preserve_scan_checkpoint else []),
                scan_batches_total=previous_run.get("scan_batches_total", 0) if resuming_scan or preserve_scan_checkpoint else 0,
                scan_batches_completed=previous_run.get("scan_batches_completed", 0) if resuming_scan or preserve_scan_checkpoint else 0,
            )
            self.store.save_run(self.record)
            self.thread = Thread(
                target=self._worker, args=(settings, api_key, model, fallback_api_key),
                daemon=True, name="glassscout-discovery",
            )
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _worker(self, settings: Settings, key: str, model=None, fallback_key: str = "") -> None:
        budget = Budget(settings, self.stop_event)
        budget.deadline = self.deadline if self.deadline is not None else float("inf")
        try:
            asyncio.run(self._run(settings, key, budget, model, fallback_key))
            budget.check()
            self.update(status="partial" if self.record.warnings else "completed", phase="Finished")
        except LimitReached as exc:
            self.update(
                status="stopped" if self.stop_event.is_set() else "partial",
                phase=str(exc), ai_status="stopped" if self.stop_event.is_set() else "paused",
                scanner_status="stopped" if self.stop_event.is_set() and self.record.scanner_status == "scanning" else self.record.scanner_status,
            )
        except ModelUnavailable as exc:
            self.warning(str(exc))
            self.update(status="partial", phase="Model unavailable — results saved", ai_status="error")
        except Exception as exc:
            self.store.error("discovery-run", exc, {"phase": self.record.phase if self.record else "unknown"})
            self.store.event("error", f"Run failed: {type(exc).__name__}")
            self.update(status="failed", phase=f"Run failed: {type(exc).__name__}", detail="See local diagnostics; existing services were kept.", ai_status="error")
        finally:
            self.update(finished_at=now(), model_calls=budget.model_calls, input_chars=budget.input_chars)

    async def _run(self, settings: Settings, key: str, budget: Budget, model=None, fallback_key: str = "") -> None:
        routes = []
        if model is not None:
            routes.append((settings.provider, settings.model, model))
        else:
            if key.strip():
                routes.append((settings.provider, settings.model, create_model(settings.provider, key, settings.model)))
            if (fallback_key.strip() and settings.fallback_provider
                    and settings.fallback_provider != settings.provider and settings.fallback_model):
                routes.append((
                    settings.fallback_provider,
                    settings.fallback_model,
                    create_model(settings.fallback_provider, fallback_key, settings.fallback_model),
                ))
        if not routes:
            raise ModelUnavailable("No configured AI provider is available.")
        active = await asyncio.to_thread(self._active_saved_endpoints) if settings.skip_active_services and settings.networks else set()
        skipped_active: set[tuple[str, int]] = set()
        loop = asyncio.get_running_loop()
        endpoint_queue: asyncio.Queue[str | object] = asyncio.Queue()
        sentinel = object()
        pending = list(dict.fromkeys(settings.seed_urls))
        known_endpoints = set(pending)
        processed_endpoints: set[str] = set()
        successful_transports: set[tuple[str, int, str]] = set()
        scan_errors: list[BaseException] = []
        queue_warning_sent = False
        endpoint_limit_sent = False
        for endpoint in pending:
            endpoint_queue.put_nowait(endpoint)

        def persist_pending() -> None:
            self.update(
                endpoints=len(known_endpoints),
                pending_urls=list(pending),
            )

        def enqueue_endpoint(endpoint: str) -> None:
            nonlocal queue_warning_sent
            if endpoint in known_endpoints or endpoint in processed_endpoints:
                return
            if not settings.unlimited_run and len(pending) >= 512:
                if not queue_warning_sent:
                    queue_warning_sent = True
                    self.warning("Resume queue capped at 512 URLs; scan a smaller subnet for complete coverage.")
                return
            known_endpoints.add(endpoint)
            pending.append(endpoint)
            endpoint_queue.put_nowait(endpoint)
            persist_pending()

        def enqueue_service(address: str, port: int) -> None:
            if (address, port) in active:
                skipped_active.add((address, port))
                self.update(skipped_active_services=len(skipped_active))
                return
            if 1 <= port <= 65535:
                # The consumer sees HTTPS first and suppresses HTTP when both
                # transports lead to the same successful endpoint.
                enqueue_endpoint(f"https://{address}:{port}/")
                enqueue_endpoint(f"http://{address}:{port}/")

        chunks = settings.scan_chunks or (chunk_ports(settings.ports) if settings.networks else [])
        batch_offset = self.record.scan_batches_completed if settings.scan_chunks else 0
        batch_total = self.record.scan_batches_total if settings.scan_chunks and self.record.scan_batches_total else len(chunks)
        initial_values = dict(
            endpoints=len(known_endpoints), pending_urls=list(pending),
            phase="Scanning and identifying services" if chunks else "Identifying services",
            scan_detail=f"{', '.join(settings.networks)} · {settings.ports}" if chunks else "",
            scanner_status="scanning" if chunks else "not requested",
            ai_status="identifying",
            active_ai_provider=routes[0][0],
            active_ai_model=routes[0][1],
        )
        if chunks:
            initial_values["pending_port_ranges"] = chunks
            initial_values["scan_batches_total"] = batch_total
            initial_values["scan_batches_completed"] = batch_offset
        self.update(**initial_values)

        def scan_worker() -> None:
            scan_budget = Budget(settings, self.stop_event)
            scan_budget.deadline = float("inf") if settings.unlimited_run else min(budget.deadline, monotonic() + settings.run_seconds * 0.4)
            with self.lock:
                self.scan_deadline = None if settings.unlimited_run else scan_budget.deadline
                self.update(scan_timeout_seconds=0 if self.scan_deadline is None else max(0, self.scan_deadline - monotonic()))

            def found(address: str, port: int) -> None:
                loop.call_soon_threadsafe(enqueue_service, address, port)

            try:
                if settings.mdns:
                    try:
                        for address, port in discover_mdns(scan_budget):
                            found(address, port)
                    except LimitReached:
                        raise
                    except Exception:
                        self.warning("mDNS unavailable; continuing with TCP discovery.")
                for index, port_range in enumerate(chunks):
                    self.update(
                        scan_detail=f"Ports {port_range} · batch {batch_offset + index + 1} of {batch_total}",
                        pending_port_ranges=chunks[index:],
                    )
                    scan_tcp(
                        scan_budget, found,
                        lambda message: self.update(scan_detail=message),
                        ports=port_range,
                    )
                    self.update(
                        pending_port_ranges=chunks[index + 1:],
                        scan_batches_completed=batch_offset + index + 1,
                    )
            except LimitReached as exc:
                if self.stop_event.is_set():
                    scan_errors.append(exc)
                else:
                    self.warning("TCP scan reached its time allocation. Coverage is partial; increase the run time or narrow the subnet to continue.")
            except BaseException as exc:
                scan_errors.append(exc)
            finally:
                with self.lock:
                    self.scan_deadline = None
                self.update(scanner_status="stopped" if self.stop_event.is_set() else ("error" if scan_errors else "completed"))
                loop.call_soon_threadsafe(endpoint_queue.put_nowait, sentinel)

        scanner = asyncio.create_task(asyncio.to_thread(scan_worker)) if chunks else None
        if scanner is None:
            endpoint_queue.put_nowait(sentinel)

        consumer_error: BaseException | None = None
        internal_stop = False
        processed = 0
        provider_cooldowns: dict[str, float] = {}

        def set_provider_cooldown(provider: str, seconds: float) -> None:
            provider_cooldowns[provider] = monotonic() + seconds
            retry_at = dict(self.record.provider_retry_at)
            retry_at[provider] = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            self.update(provider_retry_at=retry_at)

        async def wait_for_provider(seconds: float, endpoint: str, provider: str, *, quota: bool) -> None:
            wait_until = monotonic() + seconds
            reason = "quota reset" if quota else "provider recovery"
            self.store.event("provider_wait", f"{provider.title()} AI waiting for {reason} ({int(seconds + .999)} seconds).")
            last_bucket = None
            while True:
                budget.check()
                remaining = max(0, wait_until - monotonic())
                if remaining <= 0:
                    break
                # Persist occasionally so disconnected browsers and other users
                # can see why the otherwise-unbounded scan is still running.
                bucket = int(remaining // 30)
                if bucket != last_bucket:
                    last_bucket = bucket
                    self.update(
                        phase="Scanning continues · AI waiting" if scanner is not None and not scanner.done() else "AI waiting",
                        detail=f"{provider.title()} retry for {endpoint} in {int(remaining + .999)} seconds",
                        ai_status=f"waiting {int(remaining + .999)}s for {provider.title()}",
                        model_calls=budget.model_calls,
                        input_chars=budget.input_chars,
                    )
                await asyncio.sleep(min(1, remaining))

        try:
            while True:
                endpoint = await endpoint_queue.get()
                if endpoint is sentinel:
                    break
                if not isinstance(endpoint, str):
                    continue
                if not settings.unlimited_run and processed >= settings.max_endpoints:
                    if not endpoint_limit_sent:
                        endpoint_limit_sent = True
                        self.warning(f"Endpoint limit reached: {settings.max_endpoints} checked; remaining URLs saved for resume.")
                    continue
                budget.check()
                self.update(detail=endpoint)
                route_index = next((index for index, route in enumerate(routes)
                                    if provider_cooldowns.get(route[0], 0) <= monotonic()), 0)
                quota_waited: set[int] = set()
                while True:
                    provider_name, model_name, active_model = routes[route_index]
                    self.update(
                        ai_status=f"identifying with {provider_name.title()}",
                        active_ai_provider=provider_name,
                        active_ai_model=model_name,
                    )
                    try:
                        await self._identify_endpoint(
                            endpoint, settings, budget, active_model,
                            successful_transports, provider_name, model_name,
                        )
                        provider_cooldowns.pop(provider_name, None)
                        break
                    except ProviderRateLimited as exc:
                        if route_index in quota_waited and route_index + 1 < len(routes):
                            set_provider_cooldown(provider_name, max(45, exc.retry_after))
                            route_index += 1
                            self.update(fallback_count=self.record.fallback_count + 1)
                            self.store.event("provider_fallback", f"{provider_name.title()} still unavailable after its reset; switching to {routes[route_index][0].title()}.")
                            continue
                        if not settings.unlimited_run:
                            if route_index + 1 < len(routes):
                                route_index += 1
                                self.update(fallback_count=self.record.fallback_count + 1)
                                self.store.event("provider_fallback", f"Switching AI identification to {routes[route_index][0].title()}.")
                                continue
                            raise
                        quota_waited.add(route_index)
                        await wait_for_provider(exc.retry_after, endpoint, provider_name, quota=True)
                        self.update(phase="Scanning and identifying services", detail=endpoint)
                    except ModelUnavailable:
                        set_provider_cooldown(provider_name, 45)
                        if route_index + 1 < len(routes):
                            route_index += 1
                            self.update(fallback_count=self.record.fallback_count + 1)
                            self.store.event("provider_fallback", f"Switching AI identification to {routes[route_index][0].title()}.")
                            continue
                        if not settings.unlimited_run:
                            raise
                        # Keep both the scanner and persisted endpoint queue alive.
                        # A later pass starts with the selected primary provider again.
                        await wait_for_provider(45, endpoint, provider_name, quota=False)
                        route_index = 0
                processed += 1
                processed_endpoints.add(endpoint)
                if endpoint in pending:
                    pending.remove(endpoint)
                self.update(
                    processed=processed,
                    pending_urls=list(pending),
                    model_calls=budget.model_calls,
                    input_chars=budget.input_chars,
                )
                if not settings.unlimited_run and (budget.model_calls >= settings.total_model_calls or budget.input_chars >= settings.input_chars):
                    raise LimitReached("Run model/input budget reached; partial results saved")
        except BaseException as exc:
            consumer_error = exc
            internal_stop = not self.stop_event.is_set()
            # A full network scan must finish port coverage even when service
            # identification fails for a non-quota provider problem. Keep the
            # discovered addresses persisted for a later resume.
            if not settings.unlimited_run:
                self.stop_event.set()
        finally:
            if scanner is not None:
                await scanner
            if consumer_error is not None and internal_stop:
                self.stop_event.clear()

        if consumer_error is not None:
            self.update(ai_status="stopped" if self.stop_event.is_set() else "error")
            raise consumer_error
        if scan_errors:
            raise scan_errors[0]
        self.update(
            phase="Finished", processed=processed,
            pending_urls=list(pending), model_calls=budget.model_calls,
            input_chars=budget.input_chars, ai_status="completed",
        )

    async def _identify_endpoint(
        self,
        endpoint: str,
        settings: Settings,
        budget: Budget,
        model,
        successful_transports: set[tuple[str, int, str]],
        provider_name: str = "",
        model_name: str = "",
    ) -> None:
        try:
            url, address = await resolve_local(endpoint)
        except (ValueError, OSError, TimeoutError):
            if endpoint in settings.seed_urls and settings.seed_urls_are_manual:
                self.store.save_finding(Finding(endpoint=endpoint, reason="Target did not resolve to a usable local IPv4 address."))
            return
        parsed = urlsplit(url)
        transport_key = (address, parsed.port or (443 if parsed.scheme == "https" else 80), parsed.path)
        if transport_key in successful_transports:
            return
        probe = Probe(url, address, budget.service(), provider=provider_name, model=model_name)
        calls_before = budget.model_calls
        try:
            finding = await identify(probe, model, self.store.event, self.store.error)
        except ModelUnavailable as exc:
            if hasattr(exc, "finding"):
                self.store.save_finding(exc.finding)
            raise
        if probe.observations and not probe.observations[0].error:
            successful_transports.add(transport_key)
        if not probe.observations or probe.observations[0].error:
            if endpoint not in settings.seed_urls or not settings.seed_urls_are_manual:
                self.store.event("probe", f"{endpoint}: no usable web response")
                return
        if budget.model_calls > calls_before:
            finding.ai_provider = provider_name
            finding.ai_model = model_name
        saved, added = self.store.save_finding(finding)
        self.store.event(saved.state, f"{saved.endpoint}: {saved.reason}")
        self.update(added=self.record.added + int(added))

    def _active_saved_endpoints(self) -> set[tuple[str, int]]:
        """Return saved local IP/port pairs that currently accept TCP connections."""
        target = self.store.data_dir / "services.json"
        try:
            values = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
        except (OSError, ValueError):
            return set()
        candidates = []
        for service in values if isinstance(values, list) else []:
            try:
                parsed = urlsplit(str(service["url"]) if "://" in str(service["url"]) else f"http://{service['url']}")
                if not parsed.hostname:
                    continue
                candidates.append((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)))
            except (KeyError, TypeError, ValueError):
                continue

        def available(item: tuple[str, int]) -> tuple[str, int] | None:
            try:
                host, port = item
                addresses = {
                    result[4][0] for result in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
                    if ipaddress.IPv4Address(result[4][0]).is_private
                }
                for address in addresses:
                    try:
                        with socket.create_connection((address, port), timeout=0.45):
                            return address, port
                    except OSError:
                        pass
            except (OSError, ValueError):
                return None
            return None

        with ThreadPoolExecutor(max_workers=min(16, len(candidates) or 1)) as pool:
            return {item for item in pool.map(available, candidates) if item is not None}
