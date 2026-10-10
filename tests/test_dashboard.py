"""Streamlit UI checks with isolated persistence and no network calls."""

from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

try:
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None


@unittest.skipIf(AppTest is None, "Streamlit is not installed in this test interpreter")
class DashboardTests(unittest.TestCase):
    def test_board_event_on_full_run_saves_before_render_without_rerun(self):
        from dashboard import components, layout
        from dashboard.service_board import service_board_key

        script = '''
import streamlit as st
from dashboard.components import render_service_grid
st.session_state['full_runs'] = st.session_state.get('full_runs', 0) + 1
render_service_grid([{'id': 'a'}, {'id': 'b'}])
'''
        services = [{"id": "a"}, {"id": "b"}]
        reordered = {"version": 1, "items": [
            {"type": "service", "id": "b"}, {"type": "service", "id": "a"},
        ]}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            with patch.object(components, "load_layout", side_effect=lambda ids: layout.load_layout(ids, path)), patch.object(
                components, "save_layout", side_effect=lambda value, ids: layout.save_layout(value, ids, path)
            ) as save, patch.object(components, "service_board") as board:
                app = AppTest.from_string(script).run()
                # Reproduce a new component event arriving during a full run.
                app.session_state[service_board_key(services)] = {
                    "type": "layout", "nonce": "drop-1", "layout": reordered,
                }
                app.run()
                self.assertFalse(app.exception, [e.message for e in app.exception])
                self.assertEqual(app.session_state["full_runs"], 2)
                self.assertEqual(json.loads(path.read_text()), reordered)
                self.assertEqual(board.call_args.args[1], reordered)
                self.assertEqual(save.call_count, 1)
                app.run()
                self.assertFalse(app.exception)
                self.assertEqual(save.call_count, 1)

    def test_service_board_key_changes_with_service_membership(self):
        from dashboard import service_board

        with patch.object(service_board, "_service_board", return_value=None) as component:
            service_board.service_board([{"id": "a"}, {"id": "b"}], {"items": []})
            first_key = component.call_args.kwargs["key"]
            service_board.service_board([{"id": "a"}], {"items": []})
            second_key = component.call_args.kwargs["key"]
            service_board.service_board([{"id": "b"}, {"id": "a"}], {"items": []})
            reordered_key = component.call_args.kwargs["key"]

        self.assertNotEqual(first_key, second_key)
        self.assertEqual(first_key, reordered_key)

    def test_sidebar_starts_collapsed(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
        self.assertIn('initial_sidebar_state="collapsed"', source)

    def test_agent_config_reads_provider_and_model_from_secrets(self):
        from dashboard import agent_panel
        with patch.dict(agent_panel.os.environ, {}, clear=True), patch.object(
            agent_panel.st, "secrets", {"LLM_PROVIDER": "groq", "GROQ_MODEL": "configured-model"}
        ):
            self.assertEqual(agent_panel.configured_value("LLM_PROVIDER"), "groq")
            self.assertEqual(agent_panel.configured_value("GROQ_MODEL"), "configured-model")

    def test_encrypted_systemd_credential_takes_precedence(self):
        from dashboard import agent_panel
        with TemporaryDirectory() as directory:
            Path(directory, "GROQ_API_KEY").write_text("credential-key\n")
            with patch.dict(agent_panel.os.environ, {
                "CREDENTIALS_DIRECTORY": directory,
                "GROQ_API_KEY": "environment-key",
            }, clear=True), patch.object(agent_panel.st, "secrets", {"GROQ_API_KEY": "file-key"}):
                self.assertEqual(agent_panel.configured_key("groq"), "credential-key")

    def test_api_key_is_not_read_from_streamlit_file(self):
        from dashboard import agent_panel
        with patch.dict(agent_panel.os.environ, {}, clear=True), patch.object(
            agent_panel.st, "secrets", {"GROQ_API_KEY": "plaintext-file-key"}
        ):
            self.assertEqual(agent_panel.configured_key("groq"), "")

    def test_modify_service_save_cancel_and_endpoint_sync(self):
        from dashboard import agent_panel, services
        with TemporaryDirectory() as directory:
            target = Path(directory) / "services.json"
            with patch.object(services, "DATA_FILE", target), patch.object(agent_panel, "dependencies_available", return_value=False):
                services.save_services([services.clean_service({"id": "test", "name": "Old name", "url": "http://localhost:8000"})])
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=20)
                app.button(key="open_manage_services").click().run(timeout=20)
                app.button(key="modify_test").click().run()
                app.text_input(key="edit_name_test").set_value("New name")
                app.text_input(key="edit_url_test").set_value("http://localhost:9000")
                app.text_input(key="edit_kind_test").set_value("monitoring")
                app.text_area(key="edit_description_test").set_value("Updated description")
                app.selectbox(key="edit_test_icon_choice").set_value("__custom__").run()
                app.text_input(key="edit_test_custom_icon").set_value("★")
                app.selectbox(key="edit_tone_test").set_value("mint")
                next(b for b in app.button if b.label == "Save changes").click().run()
                self.assertFalse(app.exception, [e.message for e in app.exception])
                saved = json.loads(target.read_text())[0]
                self.assertEqual(saved["id"], "test")
                self.assertEqual(saved["name"], "New name")
                self.assertEqual(saved["url"], "http://localhost:9000")
                self.assertEqual(saved["kind"], "MONITORING")
                self.assertEqual(saved["description"], "Updated description")
                self.assertEqual(saved["icon"], "★")
                self.assertEqual(saved["tone"], "mint")
                self.assertEqual(app.text_input(key="endpoint_test").value, saved["url"])
                app.button(key="open_manage_services").click().run(timeout=20)
                app.button(key="modify_test").click().run()
                app.text_input(key="edit_name_test").set_value("Discard me")
                next(b for b in app.button if b.label == "Cancel").click().run()
                self.assertEqual(json.loads(target.read_text())[0]["name"], "New name")
                app.button(key="open_manage_services").click().run(timeout=20)
                app.button(key="modify_test").click().run()
                self.assertEqual(app.text_input(key="edit_name_test").value, "New name")
                app.text_input(key="edit_name_test").set_value(" ")
                next(b for b in app.button if b.label == "Save changes").click().run()
                self.assertTrue(any("required" in w.value for w in app.warning))
                self.assertEqual(json.loads(target.read_text())[0]["name"], "New name")

    def test_added_service_notification_is_one_time(self):
        from dashboard import agent_panel, services
        with TemporaryDirectory() as directory:
            target = Path(directory) / "services.json"
            with patch.object(services, "DATA_FILE", target), patch.object(agent_panel, "dependencies_available", return_value=False):
                services.save_services([])
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=20)
                app.button(key="open_add_service").click().run(timeout=20)
                app.text_input(key="add_service_name").set_value("Grafana")
                app.text_input(key="add_service_url").set_value("http://localhost:3000")
                app.selectbox(key="add_service_icon_choice").set_value("◉")
                app.selectbox(key="add_service_tone").set_value("violet")
                app.button(key="confirm_add_service").click().run(timeout=20)
                self.assertEqual([toast.value for toast in app.toast], ["Service added"])
                saved = json.loads(target.read_text())[0]
                self.assertEqual(saved["icon"], "◉")
                self.assertEqual(saved["tone"], "violet")
                app.run(timeout=20)
                self.assertFalse(app.toast)

    def test_live_scan_bars_and_countdown(self):
        script = '''
import streamlit as st
from dashboard.discovery_progress import render_progress
from discovery.models import RunRecord
record = RunRecord(phase="Scanning TCP ports", timeout_seconds=600, remaining_seconds=475,
                   scan_timeout_seconds=240, scan_remaining_seconds=115).model_dump()
render_progress(record, True)
'''
        app = AppTest.from_string(script).run()
        self.assertFalse(app.exception, [e.message for e in app.exception])
        bars = app.get("progress")
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].proto.text, "Agent timeout in 07:55")
        self.assertEqual(bars[1].proto.text, "TCP scan timeout in 01:55")
        self.assertTrue(any("not scan completion" in c.value for c in app.caption))

    def test_compact_finished_status_hides_diagnostics_and_empty_bar(self):
        script = '''
from dashboard.discovery_progress import render_progress
from discovery.models import RunRecord
record = RunRecord(status="partial", phase="Model unavailable — results saved",
                   detail="http://192.168.2.1/", endpoints=1, processed=0,
                   warnings=["Long provider diagnostic"]).model_dump()
render_progress(record, False, compact=True)
'''
        app = AppTest.from_string(script).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.get("progress"))
        self.assertFalse(app.warning)
        self.assertEqual([item.value for item in app.caption], ["Scan incomplete · 0 added"])

    def test_compact_running_status_shows_scan_and_identification_progress(self):
        script = '''
from dashboard.discovery_progress import render_progress
from discovery.models import RunRecord
record = RunRecord(phase="Scanning and identifying services", endpoints=8, processed=3,
                   scan_batches_total=64, scan_batches_completed=12).model_dump()
render_progress(record, True, compact=True)
'''
        app = AppTest.from_string(script).run()
        self.assertFalse(app.exception)
        bars = app.get("progress")
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].proto.text, "12 / 64 port batches scanned")
        self.assertEqual(bars[1].proto.text, "3 / 8 discovered addresses checked")

    def test_finished_or_interrupted_run_does_not_show_live_timeout(self):
        script = '''
from dashboard.discovery_progress import render_progress
from discovery.models import RunRecord
record = RunRecord(phase="Scanning TCP ports", timeout_seconds=600, remaining_seconds=400,
                   endpoints=4, processed=2).model_dump()
render_progress(record, False)
'''
        app = AppTest.from_string(script).run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("progress")), 1)
        self.assertIn("2 / 4", app.get("progress")[0].proto.text)
        self.assertTrue(any("interrupted" in m.value for m in app.markdown))

    def test_missing_agent_dependencies_keeps_dashboard_usable(self):
        from dashboard import agent_panel
        with patch.object(agent_panel, "dependencies_available", return_value=False):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=20)
        self.assertFalse(app.exception, [e.message for e in app.exception])
        self.assertTrue(any("requirements" in info.value for info in app.info))
        self.assertFalse(any(button.label == "Run agent" for button in app.button))

    def test_discovery_controls_and_manual_approval(self):
        from dashboard import agent_panel, services
        from discovery.models import Finding, Proposal
        from discovery.runner import JobManager
        with TemporaryDirectory() as directory:
            data = Path(directory)
            job = JobManager(data)
            job.store.save_finding(Finding(endpoint="http://127.0.0.1:48765/", proposal=Proposal(name="Possible app", url="http://127.0.0.1:48765/"), reason="Identity needs review"))
            job.store.event("react_reason", "Step 1 · Check the identity API")
            job.store.event("react_act", "Step 1 · fetch_service")
            job.store.event("react_observe", "Step 1 · HTTP 200")
            with patch.object(agent_panel, "dependencies_available", return_value=True), patch.object(agent_panel, "manager", return_value=job), patch.object(agent_panel, "networks", return_value=[]), patch.object(agent_panel, "configured_key", return_value=""), patch.object(agent_panel, "DATA_FILE", data / "services.json"), patch.object(services, "DATA_FILE", data / "services.json"):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=20)
                self.assertFalse(app.exception, [e.message for e in app.exception])
                self.assertFalse(app.text)
                self.assertFalse(any(button.label == "Approve / save changes" for button in app.button))
                app.button(key="open_discovery").click().run(timeout=20)
                self.assertTrue(any(button.label == "Start discovery" for button in app.button))
                self.assertTrue(any(button.label == "Scan entire network" for button in app.button))
                self.assertFalse(any("API key" in field.label for field in app.text_input))
                self.assertTrue(any("1. Choose where to look" in item.value for item in app.markdown))
                self.assertTrue(any("2. AI identification" in item.value for item in app.markdown))
                self.assertTrue(any(expander.label == "Advanced scan settings" for expander in app.expander))
                next(b for b in app.button if b.label == "Cancel").click().run(timeout=20)
                app.button(key="open_discovery_results").click().run(timeout=20)
                self.assertFalse(app.exception, [e.message for e in app.exception])
                self.assertFalse(any(e.label in {"Evidence (1)", "Scan diagnostics", "History"} for e in app.expander))
                self.assertTrue(any(button.label == "Download discovery report" for button in app.get("download_button")))
                approve = next(button for button in app.button if button.label == "Approve / save changes")
                approve.click().run(timeout=20)
                self.assertFalse(app.exception, [e.message for e in app.exception])
                self.assertTrue((data / "services.json").exists())
                self.assertEqual(next(iter(job.store.read()["findings"].values()))["state"], "approved")

    def test_delete_discovery_history_button_keeps_services(self):
        from dashboard import agent_panel, services
        from discovery.models import Finding
        from discovery.runner import JobManager
        with TemporaryDirectory() as directory:
            data = Path(directory)
            job = JobManager(data)
            job.store.save_finding(Finding(endpoint="http://127.0.0.1:48765/", reason="Needs review"))
            service_file = data / "services.json"
            with patch.object(services, "DATA_FILE", service_file):
                services.save_services([services.clean_service({"id": "kept", "name": "Kept", "url": "http://localhost:8000"})])
            with patch.object(agent_panel, "dependencies_available", return_value=True), patch.object(agent_panel, "manager", return_value=job), patch.object(agent_panel, "networks", return_value=[]), patch.object(agent_panel, "configured_key", return_value=""), patch.object(agent_panel, "DATA_FILE", service_file), patch.object(services, "DATA_FILE", service_file):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=20)
                app.button(key="open_discovery_results").click().run(timeout=20)
                next(button for button in app.button if button.label == "Delete history").click().run(timeout=20)
                self.assertTrue(any(button.label == "Confirm deletion" for button in app.button))
                self.assertTrue(service_file.exists())
                next(button for button in app.button if button.label == "Confirm deletion").click().run(timeout=20)
                self.assertFalse(app.exception, [e.message for e in app.exception])
                self.assertEqual(job.store.read(), {"findings": {}, "run": None, "events": []})
                self.assertEqual(json.loads(service_file.read_text())[0]["id"], "kept")

    def test_missing_key_is_a_user_error(self):
        from dashboard import agent_panel, services
        from discovery.runner import JobManager
        with TemporaryDirectory() as directory:
            data = Path(directory)
            job = JobManager(data)
            with patch.object(agent_panel, "dependencies_available", return_value=True), patch.object(agent_panel, "manager", return_value=job), patch.object(agent_panel, "networks", return_value=[]), patch.object(agent_panel, "configured_key", return_value=""), patch.object(services, "DATA_FILE", data / "services.json"):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=20)
                app.button(key="open_discovery").click().run(timeout=20)
                next(area for area in app.text_area if area.label == "Known service URLs").set_value("http://127.0.0.1:48765/")
                next(button for button in app.button if button.label == "Start discovery").click().run(timeout=20)
                self.assertFalse(app.exception, [e.message for e in app.exception])
                self.assertTrue(any("API key" in error.value for error in app.error))
                self.assertFalse(job.running)


if __name__ == "__main__":
    unittest.main()
