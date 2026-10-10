"""Offline transport tests plus a live localhost smoke test (no API fees)."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, ValidationError

from dashboard import services
from devtools.fake_service.__main__ import make_server, response_for
from discovery.agent import identify
from discovery.budget import Budget, LimitReached
from discovery.middleware import DiscoveryGuard, _is_rate_limit, _is_transient_provider_error, _retry_after_seconds
from discovery.models import EvidenceRef, FetchServiceInput, Finding, FinishServiceInput, Observation, Proposal, RunRecord, Settings, clean_url
from discovery.network import chunk_ports, parse_host, scan_tcp
from discovery.probe import Probe, redact, resolve_local
from discovery.provider import create_gemini_model, ModelUnavailable, ProviderRateLimited
from discovery.runner import JobManager
from discovery.storage import Store
from discovery.tools import service_tools
from discovery.verification import assess, verify


@contextmanager
def fixture(scenario="normal"):
    class Response:
        def __init__(self, path):
            self.status, body, content_type, headers = response_for(path, scenario)
            self.headers = {k.lower(): v for k, v in headers.items()}
            self.headers["content-type"] = content_type
            self.buffer = io.BytesIO(body.encode())
        def getheader(self, key, default=""):
            return self.headers.get(key.lower(), default)
        def read1(self, size):
            return self.buffer.read(size)
    class Socket:
        def settimeout(self, seconds):
            pass
        def shutdown(self, how):
            pass
    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = Socket()
        def connect(self):
            pass
        def request(self, method, path, headers):
            self.path = path
        def getresponse(self):
            if scenario == "slow":
                time.sleep(0.25)
                raise TimeoutError()
            return Response(self.path)
        def close(self):
            pass
    with patch("discovery.probe.http.client.HTTPConnection", Connection):
        yield "http://127.0.0.1:48765/"


@contextmanager
def live_fixture():
    try:
        server = make_server()
    except PermissionError:
        raise unittest.SkipTest("Environment does not permit binding a localhost port")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def make_probe(url, **kwargs):
    settings = Settings(seed_urls=[url], **kwargs)
    return Probe(url, "127.0.0.1", Budget(settings, Event()).service())


class ScriptedModel(BaseChatModel):
    """A LangChain chat model double, running inside the real create_agent graph."""
    mode: str = "normal"
    histories: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "glassscout-test-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("Tests exercise the async agent")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        response = self.respond(messages)
        return ChatResult(generations=[ChatGeneration(message=response)])

    def call(self, name, **args):
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": args, "id": f"call-{len(self.histories)}", "type": "tool_call",
        }], additional_kwargs={"test_signature": "preserve-me"})

    def respond(self, messages):
        self.histories.append(list(messages))
        context = json.loads(messages[-1].content)
        observations = context["observations"]
        if self.mode == "unavailable":
            raise ModelUnavailable("Test quota exhausted")
        if self.mode == "repeat":
            return self.call("fetch_service", url=context["endpoint"], summary="Repeat")
        if self.mode == "escape":
            return self.call("fetch_service", url="http://example.com/", summary="Escape attempt")
        if self.mode == "invalid":
            raise ValueError("Bad schema")
        if self.mode == "bad_args":
            return self.call("fetch_service", summary="Missing URL", invented=True)
        if self.mode == "unknown_tool":
            return self.call("run_shell", command="must not run")
        if self.mode == "plain_text":
            return AIMessage(content='{"action":"finish"}')
        if self.mode == "null_finish":
            return self.call("finish_service", summary="Not enough evidence")
        if self.mode == "repair" and len(self.histories) == 1:
            return self.call("invented_tool", summary="Rejected first attempt")
        if self.mode == "batch":
            response = self.call("fetch_service", url=context["endpoint"] + "api/info", summary="Fetch")
            response.tool_calls.append({"name": "finish_service", "args": {"summary": "Finish"}, "id": "second", "type": "tool_call"})
            return response
        if self.mode == "redirect":
            return self.call("fetch_service", url=observations[-1]["links"][0], summary="Follow local redirect")
        if not any(o["url"].endswith("/api/info") for o in observations):
            return self.call("fetch_service", url=context["endpoint"] + "api/info", summary="Check the identity API")
        name = "Proxmox VE" if self.mode == "conflict" else "GlassScout Demo"
        successful = [o for o in observations if 200 <= o["status"] < 300]
        evidence = [EvidenceRef(observation_id=o["id"], quote=o["title"] or "GlassScout Demo") for o in successful[:2]]
        return self.call("finish_service", summary="Collected evidence", proposal=Proposal(name=name, url=context["endpoint"], evidence=evidence).model_dump())


class ValidationTests(unittest.TestCase):
    def test_provider_rate_limit_delay_is_parsed_without_logging_raw_error(self):
        error = RuntimeError(
            "429 rate limit for private-org; Please try again in 20.79s."
        )
        self.assertTrue(_is_rate_limit(error))
        self.assertTrue(_is_transient_provider_error(error))
        self.assertAlmostEqual(_retry_after_seconds(error, 2), 21.04, places=2)

    def test_rate_limit_waits_and_retries_once(self):
        class Request:
            system_message = None
            messages = []
            model_settings = {}

            def override(self, **values):
                self.model_settings = values.get("model_settings", self.model_settings)
                return self

        async def exercise():
            events = []
            errors = []
            guard = DiscoveryGuard(
                make_probe("http://127.0.0.1/"),
                [],
                lambda kind, message: events.append((kind, message)),
                lambda category, error, context: errors.append((category, type(error).__name__, context)),
            )
            calls = 0

            async def handler(_request):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("429 private-org; try again in 0.01s")
                return "accepted"

            result = await guard.awrap_model_call(Request(), handler)
            return result, calls, events, errors

        result, calls, events, errors = asyncio.run(exercise())
        self.assertEqual(result, "accepted")
        self.assertEqual(calls, 2)
        self.assertEqual(errors[0][0], "provider-rate-limit")
        self.assertEqual(errors[0][2]["attempt"], "1")
        output = json.dumps(events)
        self.assertIn("Provider rate limit", output)
        self.assertNotIn("private-org", output)

    def test_unlimited_run_has_no_aggregate_model_or_input_cutoff(self):
        budget = Budget(Settings(
            seed_urls=["http://127.0.0.1/"], unlimited_run=True,
            total_model_calls=1, input_chars=1000,
        ), Event())
        budget.service().model(600)
        budget.service().model(600)
        self.assertEqual((budget.model_calls, budget.input_chars), (2, 1200))

    def test_react_action_requires_nonempty_short_summary(self):
        for summary in ("", "  \n", "x" * 241):
            with self.subTest(summary=summary), self.assertRaises(ValidationError):
                FinishServiceInput(summary=summary)
        action = FetchServiceInput(url="http://127.0.0.1/", summary="  Check identity  ")
        self.assertEqual(action.summary, "Check identity")

    def test_scope_and_port_validation(self):
        for networks in (["0.0.0.0/0"], ["8.8.8.0/24"], ["192.168.0.0/16"]):
            with self.assertRaises(ValidationError):
                Settings(networks=networks)
        for ports in ("0", "65536", "100-1", "80; echo bad", "--script=all"):
            with self.assertRaises(ValidationError):
                Settings(seed_urls=["http://127.0.0.1"], ports=ports)

    def test_urls_and_dns_scope(self):
        self.assertEqual(clean_url("HTTP://LOCALHOST:80"), "http://localhost/")
        for value in ("file:///etc/passwd", "http://user:pass@localhost", "http://localhost/?token=secret", "http://localhost:0"):
            with self.assertRaises(ValueError):
                clean_url(value)
        with self.assertRaises(ValueError):
            asyncio.run(resolve_local("http://8.8.8.8/"))

    def test_budget_cancellation_and_global_calls(self):
        budget = Budget(Settings(seed_urls=["http://127.0.0.1"], total_model_calls=1), Event())
        budget.service().model(10)
        with self.assertRaises(LimitReached):
            budget.service().model(10)
        budget.stop.set()
        with self.assertRaises(LimitReached):
            budget.check()

    def test_nmap_only_open_tcp_and_in_scope(self):
        xml = '<host><address addr="192.168.1.5" addrtype="ipv4"/><ports><port protocol="tcp" portid="18443"><state state="open"/></port><port protocol="tcp" portid="80"><state state="closed"/></port></ports></host>'
        self.assertEqual(parse_host(xml, ["192.168.1.0/24"]), [("192.168.1.5", 18443)])
        self.assertEqual(parse_host(xml, ["192.168.2.0/24"]), [])

    def test_redaction(self):
        result = redact('token="abc123" password=xyz Bearer abc.def someone@example.com org_01private')
        for secret in ("abc123", "xyz", "abc.def", "someone@example.com", "org_01private"):
            self.assertNotIn(secret, result)


class AgentTests(unittest.TestCase):
    def test_next_action_depends_on_real_tool_observation(self):
        class ObservationDrivenModel(ScriptedModel):
            def respond(self, messages):
                results = [m for m in messages if isinstance(m, ToolMessage)]
                if results and json.loads(results[-1].content)["status"] == 404:
                    self.histories.append(list(messages))
                    return self.call("finish_service", summary="Identity API returned 404; needs review")
                return super().respond(messages)

        for scenario, expected in (("normal", "verified"), ("unknown", "review")):
            with self.subTest(scenario=scenario), fixture(scenario) as url:
                model = ObservationDrivenModel()
                finding = asyncio.run(identify(make_probe(url), model, lambda *_: None))
            self.assertEqual(finding.state, expected)
            self.assertEqual(len(model.histories), 2)
            if scenario == "unknown":
                self.assertIsNone(finding.proposal)
                self.assertIn("404", finding.reason)

    def test_react_trace_follows_actual_tool_execution(self):
        events = []
        with fixture() as url:
            finding = asyncio.run(identify(make_probe(url), ScriptedModel(), lambda kind, message: events.append((kind, message))))
        self.assertEqual(finding.state, "verified")
        self.assertEqual([kind for kind, _ in events], [
            "react_reason", "react_act", "react_observe",
            "react_reason", "react_act", "react_observe", "react_finish",
        ])
        self.assertIn("Step 1", events[0][1])
        self.assertIn("fetch_service", events[1][1])
        self.assertIn("HTTP 200", events[2][1])
        self.assertIn("Step 2", events[3][1])
        self.assertIn("finish_service", events[4][1])

    def test_rejected_batch_does_not_claim_an_action_ran(self):
        events = []
        with fixture() as url:
            asyncio.run(identify(make_probe(url), ScriptedModel(mode="batch"), lambda kind, message: events.append((kind, message))))
        self.assertEqual([kind for kind, _ in events], ["react_rejected", "react_rejected", "react_stop"])

    def test_trace_redacts_summary_and_does_not_log_model_thoughts(self):
        class SecretModel(ScriptedModel):
            def respond(self, messages):
                response = self.call("finish_service", summary="Unknown; token=secret123")
                response.content = "PRIVATE-THOUGHT-MUST-NOT-BE-LOGGED"
                return response

        events = []
        with fixture() as url:
            asyncio.run(identify(make_probe(url), SecretModel(), lambda kind, message: events.append((kind, message))))
        output = json.dumps(events)
        self.assertNotIn("secret123", output)
        self.assertNotIn("PRIVATE-THOUGHT-MUST-NOT-BE-LOGGED", output)

    def run_agent(self, url, mode="normal", **kwargs):
        probe = make_probe(url, **kwargs)
        finding = asyncio.run(identify(probe, ScriptedModel(mode=mode), lambda *_: None))
        return finding, probe

    def test_normal_service_is_verified_with_fresh_requests(self):
        with fixture() as url:
            finding, probe = self.run_agent(url)
        self.assertEqual(finding.state, "verified")
        self.assertEqual(probe.budget.model_calls, 2)
        self.assertEqual(probe.budget.tool_calls, 4)
        self.assertNotIn("must-never-reach-the-model", finding.model_dump_json())

    def test_unknown_needs_review(self):
        with fixture("unknown") as url:
            finding, _ = self.run_agent(url)
        self.assertEqual(finding.state, "review")

    def test_conflicting_identity_needs_review(self):
        with fixture("conflict") as url:
            finding, _ = self.run_agent(url, "conflict")
        self.assertEqual(finding.state, "review")
        self.assertIn("disagree", finding.reason)

    def test_repeat_and_escape_stop_without_extra_requests(self):
        for mode in ("repeat", "escape", "invalid", "bad_args", "unknown_tool", "plain_text", "batch"):
            with self.subTest(mode=mode), fixture() as url:
                finding, probe = self.run_agent(url, mode)
                self.assertEqual(finding.state, "review")
                self.assertEqual(probe.budget.model_calls, 2)
                self.assertEqual(probe.budget.tool_calls, 1)
                self.assertIn("No new evidence", finding.reason)

    def test_native_tool_history_preserves_call_and_result(self):
        model = ScriptedModel()
        with fixture() as url:
            result = asyncio.run(identify(make_probe(url), model, lambda *_: None))
        self.assertEqual(result.state, "verified")
        history = model.histories[1]
        call = next(m for m in history if isinstance(m, AIMessage))
        observation = next(m for m in history if isinstance(m, ToolMessage))
        self.assertEqual(call.additional_kwargs["test_signature"], "preserve-me")
        self.assertEqual(observation.tool_call_id, call.tool_calls[0]["id"])
        self.assertEqual(observation.name, "fetch_service")
        self.assertIn("GlassScout Demo", observation.content)
        self.assertNotIn("instance_id", observation.content)

    def test_finish_without_proposal_exits_graph_without_another_model_call(self):
        with fixture() as url:
            finding, probe = self.run_agent(url, "null_finish")
        self.assertEqual(finding.state, "review")
        self.assertEqual(finding.reason, "Not enough evidence")
        self.assertEqual(probe.budget.model_calls, 1)
        self.assertEqual(probe.budget.tool_calls, 1)

    def test_framework_repair_removes_unmatched_tool_calls(self):
        model = ScriptedModel(mode="repair")
        with fixture() as url:
            probe = make_probe(url)
            result = asyncio.run(identify(probe, model, lambda *_: None))
        self.assertEqual(result.state, "verified")
        self.assertEqual(probe.budget.model_calls, 3)
        for history in model.histories[1:]:
            self.assertFalse(any(c["name"] == "invented_tool" for m in history if isinstance(m, AIMessage) for c in m.tool_calls))

    def test_stop_cancels_in_flight_model_in_framework(self):
        cancelled = Event()

        class HangingModel(ScriptedModel):
            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                try:
                    await asyncio.sleep(30)
                finally:
                    cancelled.set()

        async def run(probe):
            task = asyncio.create_task(identify(probe, HangingModel(), lambda *_: None))
            while probe.budget.model_calls == 0:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.01)
            probe.budget.run.stop.set()
            return await asyncio.wait_for(task, timeout=2)

        with fixture() as url:
            result = asyncio.run(run(make_probe(url)))
        self.assertEqual(result.state, "review")
        self.assertIn("Stopped", result.reason)
        self.assertTrue(cancelled.is_set())

    def test_fresh_verification_limit_keeps_proposal_for_review(self):
        with fixture() as url:
            probe = make_probe(url)
            original = probe.fetch

            async def bounded_fetch(target, **kwargs):
                if probe.budget.tool_calls >= 2:
                    raise LimitReached("Test verification limit reached")
                return await original(target, **kwargs)

            with patch.object(probe, "fetch", bounded_fetch):
                result = asyncio.run(identify(probe, ScriptedModel(), lambda *_: None))
        self.assertEqual(result.state, "review")
        self.assertIsNotNone(result.proposal)
        self.assertIn("verification limit", result.reason)

    def test_decorated_tools_expose_only_validated_arguments(self):
        tools = service_tools(make_probe("http://127.0.0.1:8765/"))
        self.assertEqual(set(tools), {"fetch_service", "finish_service"})
        self.assertEqual(set(tools["fetch_service"].args), {"url", "summary"})
        self.assertEqual(set(tools["finish_service"].args), {"summary", "proposal"})
        with self.assertRaises(ValidationError):
            asyncio.run(tools["fetch_service"].ainvoke({"summary": "Missing URL"}))

    def test_redirect_loop_stops(self):
        with fixture("redirect-loop") as url:
            finding, probe = self.run_agent(url, "redirect")
        self.assertEqual(finding.state, "review")
        self.assertLessEqual(probe.budget.tool_calls, 2)

    def test_model_limit_does_not_promote_a_guess(self):
        with fixture() as url:
            finding, _ = self.run_agent(url, model_calls=1)
        self.assertEqual(finding.state, "review")
        self.assertIn("limit", finding.reason)

    def test_timeout_is_bounded(self):
        with fixture("slow") as url:
            started = time.monotonic()
            finding, _ = self.run_agent(url, request_seconds=0.2)
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(finding.state, "review")
        self.assertIn("failed", finding.reason)

    def test_injection_cannot_fetch_external_origin(self):
        with fixture("injection") as url:
            finding, probe = self.run_agent(url, "escape")
        self.assertEqual(probe.budget.tool_calls, 1)
        self.assertEqual(finding.state, "review")

    def test_fabricated_evidence_cannot_verify(self):
        with fixture() as url:
            probe = make_probe(url)
            async def run():
                await probe.fetch(url)
                return await verify(Proposal(name="GlassScout Demo", url=url, evidence=[EvidenceRef(observation_id="invented", quote="GlassScout Demo")]), probe)
            passed, reason = asyncio.run(run())
        self.assertFalse(passed)
        self.assertIn("does not exist", reason)

    def test_arbitrary_paths_and_query_secrets_are_rejected(self):
        probe = make_probe("http://127.0.0.1:8765/")
        for url in ("http://127.0.0.1:8765/admin/reboot", "http://127.0.0.1:8765/?token=secret", "http://127.0.0.1:8766/"):
            with self.assertRaises(ValueError):
                probe.validate(url)


class StorageTests(unittest.TestCase):
    def test_services_backup_round_trip_replaces_validated_list(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "services.json"
            original = [services.clean_service({"id": "printer", "name": "Printer", "url": "http://192.168.2.55/"})]
            with patch.object(services, "DATA_FILE", target):
                payload = services.services_backup(original)
                services.save_services([services.clean_service({"id": "temporary", "name": "Temporary", "url": "http://localhost/"})])
                restored = services.restore_services_backup(payload)
            self.assertEqual(restored, original)
            self.assertEqual(json.loads(target.read_text()), original)

    def test_invalid_backup_does_not_replace_services(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "services.json"
            with patch.object(services, "DATA_FILE", target):
                services.save_services([services.clean_service({"id": "kept", "name": "Kept", "url": "http://localhost/"})])
                before = target.read_text()
                with self.assertRaises(ValueError):
                    services.restore_services_backup('{"services":[{"name":"Broken"}]}')
            self.assertEqual(target.read_text(), before)

    def test_modify_preserves_other_services_and_identity(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "services.json"
            with patch.object(services, "DATA_FILE", target):
                services.save_services([services.clean_service({"id": "existing", "name": "Original", "url": "http://localhost:8000"})])
                services.mutate_services(add={"id": "background", "name": "Discovered", "url": "http://localhost:9000"})
                services.mutate_services(updates={"existing": {"id": "do-not-change", "name": "Updated"}})
                saved = json.loads(target.read_text())
                self.assertEqual([s["id"] for s in saved], ["existing", "background"])
                self.assertEqual(saved[0]["name"], "Updated")
                self.assertEqual(saved[1]["name"], "Discovered")
                services.mutate_services(remove="existing")
                with self.assertRaises(ValueError):
                    services.mutate_services(updates={"existing": {"name": "Resurrect"}})
                self.assertEqual(len(json.loads(target.read_text())), 1)

    def test_snapshot_counts_down_using_worker_deadlines(self):
        with TemporaryDirectory() as directory:
            job = JobManager(Path(directory))
            job.record = RunRecord(timeout_seconds=600, scan_timeout_seconds=240)
            job.thread = Mock()
            job.thread.is_alive.return_value = True
            job.deadline, job.scan_deadline = 700, 340
            with patch("discovery.runner.monotonic", return_value=110):
                snapshot = job.snapshot()
                self.assertEqual(snapshot.remaining_seconds, 590)
                self.assertEqual(snapshot.scan_remaining_seconds, 230)
            with patch("discovery.runner.monotonic", return_value=710):
                self.assertEqual(job.snapshot().remaining_seconds, 0)
                self.assertEqual(job.snapshot().scan_remaining_seconds, 0)
            job.record.status = "stopped"
            self.assertIsNone(job.snapshot().remaining_seconds)
            self.assertIsNone(job.snapshot().scan_remaining_seconds)
            self.assertIsNone(job.record.remaining_seconds)

    def finding(self):
        url = "http://127.0.0.1:8765/"
        return Finding(endpoint=url, proposal=Proposal(name="Test service", url=url), state="verified", reason="Test verifier")

    def test_upsert_is_idempotent(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            _, first = store.save_finding(self.finding())
            _, second = store.save_finding(self.finding())
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(len(json.loads((Path(directory) / "services.json").read_text())), 1)

    def test_same_service_on_one_device_prefers_https_without_duplicates(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            http = Finding(
                endpoint="http://192.168.2.55/",
                proposal=Proposal(name="Konica Minolta", url="http://192.168.2.55/"),
                state="verified", reason="verified",
            )
            https = Finding(
                endpoint="https://192.168.2.55:8443/",
                proposal=Proposal(name="Konica Minolta", url="https://192.168.2.55:8443/"),
                state="verified", reason="verified",
            )
            _, first = store.save_finding(http)
            _, second = store.save_finding(https)
            cards = json.loads((Path(directory) / "services.json").read_text())
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["url"], "https://192.168.2.55:8443/")

    def test_same_named_http_panels_on_one_device_collapse_to_one_card(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            for url in ("http://192.168.2.55/", "http://192.168.2.55:631/"):
                store.save_finding(Finding(
                    endpoint=url, proposal=Proposal(name="Konica Minolta", url=url),
                    state="verified", reason="verified",
                ))
            cards = json.loads((Path(directory) / "services.json").read_text())
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["url"], "http://192.168.2.55/")
    def test_rejection_is_remembered(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            finding = self.finding()
            finding.state = "review"
            store.save_finding(finding)
            store.review(finding.endpoint, approve=False)
            saved, added = store.save_finding(self.finding())
            self.assertEqual(saved.state, "rejected")
            self.assertFalse(added)

    def test_manual_correction_renames_an_existing_card(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root)
            finding, _ = store.save_finding(self.finding())
            store.review(finding.endpoint, approve=True, name="Corrected name", url=finding.endpoint)
            cards = json.loads((root / "services.json").read_text())
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["name"], "Corrected name")

    def test_manual_name_and_url_survive_rescan(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            finding = self.finding()
            finding.state = "review"
            store.save_finding(finding)
            store.review(finding.endpoint, approve=True, name="My server", url="http://127.0.0.1:8765/panel")
            saved, added = store.save_finding(self.finding())
            self.assertEqual(saved.state, "approved")
            self.assertEqual(saved.proposal.name, "My server")
            self.assertFalse(added)

    def test_ui_update_keeps_background_additions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root)
            finding, _ = store.save_finding(self.finding())
            with patch.object(services, "DATA_FILE", root / "services.json"):
                services.mutate_services(add={"id": "manual", "name": "Manual", "url": "http://localhost:8000"})
                services.mutate_services(endpoints={"manual": "http://localhost:8001"})
            saved = json.loads((root / "services.json").read_text())
            self.assertEqual(len(saved), 2)
            self.assertTrue(any(s["id"] == finding.service_id for s in saved))

    def test_corrupt_data_is_not_overwritten(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "services.json").write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                Store(root).save_finding(self.finding())
            self.assertEqual((root / "services.json").read_text(), "broken")

    def test_clear_history_preserves_services(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root)
            store.save_finding(self.finding())
            store.event("probe", "A diagnostic event")
            error_file = store.error("provider-rate-limit", RuntimeError("429 token=secret org_01private"))
            self.assertTrue(error_file.exists())
            services_before = (root / "services.json").read_text(encoding="utf-8")
            store.clear_history()
            self.assertEqual(store.read(), {"findings": {}, "run": None, "events": []})
            self.assertEqual((root / "services.json").read_text(encoding="utf-8"), services_before)
            self.assertFalse(error_file.exists())

    def test_error_log_uses_err_extension_and_redacts_provider_secrets(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory))
            target = store.error(
                "provider-rate-limit",
                RuntimeError("429 org_01private token=supersecret; try again in 20.79s"),
                {"provider": "groq", "model": "openai/gpt-oss-120b"},
            )
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(target.suffix, ".err")
            self.assertEqual(target.parent.name, "errors")
            self.assertEqual(payload["category"], "provider-rate-limit")
            self.assertEqual(payload["context"]["provider"], "groq")
            self.assertIn("20.79s", payload["message"])
            serialized = json.dumps(payload)
            self.assertNotIn("org_01private", serialized)
            self.assertNotIn("supersecret", serialized)
            self.assertTrue(any(event["kind"] == "error_log" for event in store.read()["events"]))

    def test_background_end_to_end(self):
        with TemporaryDirectory() as directory, fixture() as url:
            job = JobManager(Path(directory))
            job.start(Settings(seed_urls=[url]), "", model=ScriptedModel())
            job.thread.join(timeout=5)
            self.assertFalse(job.running)
            self.assertEqual(job.snapshot().status, "completed")
            self.assertEqual(job.snapshot().added, 1)
            self.assertEqual(len(json.loads((Path(directory) / "services.json").read_text())), 1)

    def test_provider_error_preserves_evidence_and_stops_run(self):
        with TemporaryDirectory() as directory, fixture() as url:
            job = JobManager(Path(directory))
            job.start(Settings(seed_urls=[url, "http://127.0.0.1:9876/"]), "", model=ScriptedModel(mode="unavailable"))
            job.thread.join(timeout=5)
            self.assertEqual(job.snapshot().status, "partial")
            self.assertEqual(job.snapshot().model_calls, 1)
            saved = list(job.store.read()["findings"].values())
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["state"], "review")
            self.assertTrue(saved[0]["observations"])

    def test_stop_is_not_reported_as_success(self):
        with TemporaryDirectory() as directory, fixture() as url:
            job = JobManager(Path(directory))
            class StopModel(ScriptedModel):
                def respond(self, messages):
                    job.stop()
                    raise LimitReached("Stopped by user")
            job.start(Settings(seed_urls=[url]), "", model=StopModel())
            job.thread.join(timeout=5)
            self.assertEqual(job.snapshot().status, "stopped")


class ProviderTests(unittest.TestCase):
    def test_langchain_tool_calls_disable_sdk_auto_execution(self):
        from google.genai import types
        from google.genai.models import AsyncModels

        response = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(
                function_call=types.FunctionCall(name="finish_service", args={"summary": "Unknown"}),
            )]), finish_reason="STOP",
        )])
        transport = AsyncMock(return_value=response)

        async def run():
            model = create_gemini_model("test-key-not-a-real-credential", "gemini-3.6-flash")
            probe = make_probe("http://127.0.0.1:8765/")
            try:
                # Mock only the HTTP boundary: real LangChain and SDK logic run.
                with fixture(), patch.object(AsyncModels, "_generate_content", transport):
                    return await identify(probe, model, lambda *_: None)
            finally:
                await model.async_client.aclose()
                model.client.close()

        with self.assertNoLogs("google_genai.models", level="WARNING"):
            result = asyncio.run(run())
        transport.assert_awaited_once()
        config = transport.call_args.kwargs["config"]
        self.assertTrue(config.automatic_function_calling.disable)
        self.assertEqual(config.tool_config.function_calling_config.mode, "ANY")
        declarations = {f.name for t in config.tools for f in t.function_declarations}
        self.assertEqual(declarations, {"fetch_service", "finish_service"})
        self.assertEqual(result.state, "review")
        self.assertEqual(result.reason, "Unknown")


class ScannerTests(unittest.TestCase):
    def test_port_ranges_are_split_into_resumable_batches(self):
        self.assertEqual(chunk_ports("1-2050"), ["1-1024", "1025-2048", "2049-2050"])
        self.assertEqual(chunk_ports("80,443,8000-8002", size=2), ["80,443", "8000-8001", "8002"])

    def test_interrupted_port_batch_is_persisted_for_resume(self):
        with TemporaryDirectory() as directory, patch("discovery.runner.shutil.which", return_value="/test/nmap"):
            job = JobManager(Path(directory))
            settings = Settings(networks=["192.168.2.0/24"], ports="1-2050", mdns=False)
            with patch("discovery.runner.scan_tcp", side_effect=[None, LimitReached("scan timeout")]):
                job.start(settings, "", model=ScriptedModel())
                job.thread.join(5)
            self.assertEqual(job.snapshot().status, "partial")
            self.assertEqual(job.snapshot().pending_port_ranges, ["1025-2048", "2049-2050"])
            self.assertEqual((job.snapshot().scan_batches_completed, job.snapshot().scan_batches_total), (1, 3))

            address_resume = JobManager(Path(directory))
            address_resume.start(
                Settings(seed_urls=["http://127.0.0.1:9/"], seed_urls_are_manual=False),
                "", model=ScriptedModel(),
            )
            address_resume.thread.join(5)
            self.assertEqual(address_resume.snapshot().pending_port_ranges, ["1025-2048", "2049-2050"])

            replacement = JobManager(Path(directory))
            resume = Settings(
                networks=["192.168.2.0/24"], ports="1-2050", mdns=False,
                scan_chunks=address_resume.snapshot().pending_port_ranges,
            )
            with patch("discovery.runner.scan_tcp") as scan:
                replacement.start(resume, "", model=ScriptedModel())
                replacement.thread.join(5)
            self.assertEqual(replacement.snapshot().status, "completed")
            self.assertEqual(replacement.snapshot().pending_port_ranges, [])
            self.assertEqual((replacement.snapshot().scan_batches_completed, replacement.snapshot().scan_batches_total), (3, 3))
            self.assertEqual(scan.call_count, 2)

    def test_scanning_and_identification_run_concurrently(self):
        identification_started = Event()

        async def identify_during_scan(probe, _model, _event, _error):
            identification_started.set()
            probe.observations.append(Observation(url=probe.endpoint, status=200, content_type="text/html"))
            return Finding(
                endpoint=probe.endpoint,
                proposal=Proposal(name="Concurrent panel", url=probe.endpoint),
                state="verified", reason="verified",
                observations=probe.observations,
            )

        def scan(_budget, found, _progress, *, ports):
            found("127.0.0.1", 48765)
            if not identification_started.wait(2):
                raise AssertionError("identification did not start while scanning was active")

        with TemporaryDirectory() as directory, \
                patch("discovery.runner.shutil.which", return_value="/test/nmap"), \
                patch("discovery.runner.scan_tcp", side_effect=scan), \
                patch("discovery.runner.identify", side_effect=identify_during_scan):
            job = JobManager(Path(directory))
            job.start(
                Settings(networks=["127.0.0.1/32"], ports="48765", mdns=False),
                "", model=ScriptedModel(),
            )
            job.thread.join(5)
            self.assertFalse(job.running)
            self.assertEqual(job.snapshot().status, "completed")
            self.assertEqual(job.snapshot().scan_batches_completed, 1)
            self.assertEqual(job.snapshot().added, 1)

    def test_full_scan_waits_for_quota_and_retries_same_endpoint(self):
        attempts = []

        def scan(_budget, found, _progress, *, ports):
            found("127.0.0.1", 48765)

        async def identify(endpoint, *_args, **_kwargs):
            attempts.append(endpoint)
            if len(attempts) == 1:
                raise ProviderRateLimited(.01)

        with TemporaryDirectory() as directory, \
                patch("discovery.runner.shutil.which", return_value="/test/nmap"), \
                patch("discovery.runner.scan_tcp", side_effect=scan), \
                patch.object(JobManager, "_identify_endpoint", side_effect=identify):
            job = JobManager(Path(directory))
            job.start(Settings(
                networks=["127.0.0.1/32"], ports="48765", mdns=False,
                unlimited_run=True, max_endpoints=1, total_model_calls=1,
                input_chars=1000,
            ), "", model=ScriptedModel())
            job.thread.join(5)
            events = job.store.read()["events"]

        self.assertFalse(job.running)
        self.assertEqual(job.snapshot().status, "completed")
        self.assertEqual(job.snapshot().processed, 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertTrue(any(event["kind"] == "provider_wait" for event in events))

    def test_provider_failure_does_not_interrupt_full_port_coverage(self):
        identification_failed = Event()
        scanned = []

        def scan(budget, found, _progress, *, ports):
            scanned.append(ports)
            if len(scanned) == 1:
                found("127.0.0.1", 48765)
                if not identification_failed.wait(2):
                    raise AssertionError("identification failure was not observed")
                budget.check()

        async def identify(*_args, **_kwargs):
            identification_failed.set()
            raise ModelUnavailable("provider unavailable")

        with TemporaryDirectory() as directory, \
                patch("discovery.runner.shutil.which", return_value="/test/nmap"), \
                patch("discovery.runner.scan_tcp", side_effect=scan), \
                patch.object(JobManager, "_identify_endpoint", side_effect=identify):
            job = JobManager(Path(directory))
            job.start(Settings(
                networks=["127.0.0.1/32"], ports="1-2048", mdns=False,
                unlimited_run=True,
            ), "", model=ScriptedModel())
            job.thread.join(5)

        self.assertFalse(job.running)
        self.assertEqual(job.snapshot().status, "partial")
        self.assertEqual(scanned, ["1-1024", "1025-2048"])
        self.assertEqual(job.snapshot().pending_port_ranges, [])
        self.assertEqual(job.snapshot().scan_batches_completed, 2)

    def test_cancel_keeps_completed_hosts_and_terminates_process(self):
        budget = Budget(Settings(networks=["192.168.1.0/24"]), Event())
        arguments = []
        class Process:
            returncode = None
            def __init__(self, args, stdout, stderr):
                arguments.extend(args)
                stdout.write('<nmaprun><host><address addr="192.168.1.5" addrtype="ipv4"/><ports><port protocol="tcp" portid="18443"><state state="open"/></port></ports></host>')
                stdout.flush()
                budget.stop.set()
            def poll(self):
                return self.returncode
            def terminate(self):
                self.returncode = -15
            def wait(self, timeout):
                return self.returncode
        found = []
        with patch("discovery.network.shutil.which", return_value="/test/nmap"), patch("discovery.network.subprocess.Popen", Process):
            with self.assertRaises(LimitReached):
                scan_tcp(budget, lambda ip, port: found.append((ip, port)), lambda _: None)
        self.assertEqual(found, [("192.168.1.5", 18443)])
        self.assertIn("-Pn", arguments)
        self.assertIn("1-65535", arguments)


class LiveHTTPTests(unittest.TestCase):
    def test_real_local_http_server(self):
        with live_fixture() as url:
            probe = make_probe(url)
            result = asyncio.run(identify(probe, ScriptedModel(), lambda *_: None))
            self.assertEqual(result.state, "verified")


class GroqProviderTests(unittest.TestCase):
    def test_groq_model_is_created_without_network(self):
        from discovery.provider import create_model
        model = create_model("groq", "test-key-not-a-real-credential", "llama-3.3-70b-versatile")
        self.assertEqual(type(model).__name__, "ChatGroq")

    def test_forced_tool_choice_maps_to_required_on_groq(self):
        from discovery.provider import create_model
        model = create_model("groq", "test-key-not-a-real-credential", "llama-3.3-70b-versatile")
        from langchain_core.tools import tool

        @tool
        def fetch_service(url: str) -> str:
            """Fetch a URL."""
            return url

        bound = model.bind_tools([fetch_service], tool_choice="any")
        self.assertEqual(bound.kwargs["tool_choice"], "required")

    def test_provider_setting_defaults_to_gemini(self):
        url = ["http://127.0.0.1:8765/"]
        self.assertEqual(Settings(seed_urls=url).provider, "gemini")
        self.assertEqual(Settings(seed_urls=url, provider="groq").provider, "groq")
        with self.assertRaises(ValidationError):
            Settings(seed_urls=url, provider="other")


if __name__ == "__main__":
    unittest.main()
