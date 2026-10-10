"""HTTP enrichment, corroborated fingerprints, and resumable discovery."""

import asyncio
from contextlib import contextmanager
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_discovery import make_probe, fixture, ScriptedModel
from discovery.agent import identify
from discovery.browser import permitted_request
from discovery.models import Observation, Settings
from discovery.network import parse_host
from discovery.probe import Page, Probe
from discovery.probe import observation_for_model
from discovery.runner import JobManager
from discovery.middleware import _retry_after_seconds


@contextmanager
def responses(routes):
    def request(probe, url, timeout):
        from urllib.parse import urlsplit
        status, content_type, body, headers = routes.get(urlsplit(url).path, (404, 'text/html', 'Not Found', {}))
        if callable(body):
            body = body()
        obs = Observation(url=url, status=status, content_type=content_type)
        class Response:
            def getheader(self, key, default=''):
                return headers.get(key, default)
        probe._extract(obs, body.encode(), Response())
        return obs
    with patch.object(Probe, '_request', request):
        yield


class EnrichmentTests(unittest.TestCase):
    def test_model_observation_is_bounded_but_local_evidence_is_not_changed(self):
        observation = Observation(url="http://127.0.0.1/", status=200,
                                  content_type="text/html", text="x" * 6000,
                                  links=[f"http://127.0.0.1/{n}" for n in range(40)])
        compact = observation_for_model(observation)
        self.assertEqual(len(compact["text"]), 900)
        self.assertEqual(len(compact["links"]), 12)
        self.assertEqual(len(observation.text), 6000)
        self.assertEqual(len(observation.links), 40)

    def test_long_rate_limit_is_not_shortened(self):
        self.assertEqual(_retry_after_seconds(RuntimeError('429 try again in 2m3.5s'), 2), 123.75)
        self.assertEqual(_retry_after_seconds(RuntimeError('429 try again in 500ms'), 2), .75)
    def test_meta_and_literal_js_redirects_preserve_scope(self):
        probe = make_probe('http://127.0.0.1/')
        obs = Observation(url=probe.endpoint, status=200, content_type='text/html')
        class Response:
            def getheader(self, key, default=''):
                return default
        probe._extract(obs, b'''<meta http-equiv="refresh" content="0; URL=/login.html">
            <script>window.location.replace('/panel.html');</script>
            <script>location.href='/other.html';</script>
            <a href="http://external.example/">outside</a>
            <meta http-equiv="refresh" content="0;url=/reboot">
            <meta name="generator" content="Example Firmware">
            <form action="/login" method="post"><input value="secret"></form>''', Response())
        self.assertEqual(probe.redirects[probe.endpoint], [probe.endpoint + p for p in ('login.html', 'panel.html', 'other.html')])
        self.assertIn('Example Firmware', obs.text)
        self.assertNotIn('secret', obs.text)
        self.assertNotIn('http://external.example/', probe.allowed)
        self.assertNotIn(probe.endpoint + 'reboot', probe.allowed)

    def test_known_service_verified_without_ai(self):
        routes = {
            '/': (200, 'text/html', '<title>Proxmox Virtual Environment</title>', {}),
            '/api2/json/version': (200, 'application/json', '{"data":{"version":"9.2","release":"1","repoid":"abc"}}', {}),
        }
        with responses(routes):
            model = ScriptedModel(mode='unavailable')
            finding = asyncio.run(identify(make_probe('http://127.0.0.1/'), model, lambda *_: None))
        self.assertEqual(finding.state, 'verified')
        self.assertEqual(finding.proposal.name, 'Proxmox VE')
        self.assertEqual(model.histories, [])
        self.assertEqual(len(finding.observations), 4)

    def test_router_redirect_and_separate_script_can_verify(self):
        routes = {
            '/': (200, 'text/html', '<script>location.href="/login.html";</script>', {}),
            '/login.html': (200, 'text/html', '<title>ASUS Router</title><script src="/firmware.js"></script>', {}),
            '/firmware.js': (200, 'application/javascript', 'const firmware = "ASUSWRT";', {}),
        }
        with responses(routes):
            finding = asyncio.run(identify(make_probe('http://127.0.0.1/'), ScriptedModel(mode='unavailable'), lambda *_: None))
        self.assertEqual(finding.state, 'verified')
        self.assertEqual(finding.proposal.url, 'http://127.0.0.1/login.html')
        self.assertEqual(finding.proposal.name, 'ASUS Router')

    def test_router_redirect_to_branded_login_can_verify_without_ai(self):
        routes = {
            '/': (200, 'text/html', '<script>location.href="/Main_Login.asp";</script>', {}),
            '/Main_Login.asp': (200, 'text/html', '<title>ASUS Login</title><h1>RT-BE92U</h1>', {}),
        }
        with responses(routes):
            model = ScriptedModel(mode='unavailable')
            finding = asyncio.run(identify(make_probe('http://127.0.0.1/'), model, lambda *_: None))
        self.assertEqual(finding.state, 'verified')
        self.assertEqual(finding.proposal.url, 'http://127.0.0.1/Main_Login.asp')
        self.assertEqual(finding.proposal.name, 'ASUS Router')
        self.assertEqual(model.histories, [])

    def test_unknown_router_vendor_can_verify_from_title_and_redirect(self):
        routes = {
            '/': (302, 'text/html', '', {'location': '/admin/'}),
            '/admin/': (200, 'text/html', '<title>Acme Mesh Router</title>', {}),
        }
        with responses(routes):
            model = ScriptedModel(mode='unavailable')
            finding = asyncio.run(identify(make_probe('http://127.0.0.1/'), model, lambda *_: None))
        self.assertEqual(finding.state, 'verified')
        self.assertEqual(finding.proposal.name, 'Acme Mesh Router')
        self.assertEqual(finding.proposal.url, 'http://127.0.0.1/admin/')
        self.assertEqual(model.histories, [])

    def test_additional_router_firmware_signatures_are_recognized(self):
        from discovery.verification import router_identity
        samples = {
            'Ubiquiti UniFi': 'UniFi Network Console',
            'FRITZ!Box Router': 'FRITZ!Box 7590',
            'Synology Router': 'Synology Router Manager',
            'GL.iNet Router': 'GL.iNet Admin Panel',
            'Keenetic Router': 'Keenetic Web Interface',
            'pfSense Router': 'pfSense - Login',
            'OPNsense Router': 'OPNsense Login',
        }
        for expected, title in samples.items():
            with self.subTest(title=title):
                observation = Observation(url='http://127.0.0.1/', status=200,
                                          content_type='text/html', title=title)
                self.assertEqual(router_identity(observation), expected)

    def test_router_redirect_must_still_exist_on_fresh_fetch(self):
        root_values = iter([
            '<script>location.href="/Main_Login.asp";</script>',
            '<title>Generic landing page</title>',
        ])
        routes = {
            '/': (200, 'text/html', lambda: next(root_values), {}),
            '/Main_Login.asp': (200, 'text/html', '<title>ASUS Login</title>', {}),
        }
        with responses(routes):
            finding = asyncio.run(identify(make_probe('http://127.0.0.1/'), ScriptedModel(mode='null_finish'), lambda *_: None))
        self.assertEqual(finding.state, 'review')

    def test_router_brand_on_one_page_does_not_auto_add(self):
        routes = {'/': (200, 'text/html', '<title>ASUS Router</title>', {})}
        with responses(routes):
            finding = asyncio.run(identify(make_probe('http://127.0.0.1/'), ScriptedModel(mode='null_finish'), lambda *_: None))
        self.assertEqual(finding.state, 'review')
        self.assertEqual(finding.proposal.name, 'ASUS Router')

    def test_changed_firmware_on_fresh_fetch_is_not_verified(self):
        values = iter(['ASUSWRT', 'RouterOS'])
        routes = {
            '/': (200, 'text/html', '<title>ASUS Router</title><script src="/firmware.js"></script>', {}),
            '/firmware.js': (200, 'application/javascript', lambda: next(values), {}),
        }
        with responses(routes):
            finding = asyncio.run(identify(make_probe('http://127.0.0.1/'), ScriptedModel(mode='null_finish'), lambda *_: None))
        self.assertEqual(finding.state, 'review')

    def test_browser_policy_blocks_external_and_mutating_requests(self):
        probe = make_probe('http://127.0.0.1/')
        self.assertTrue(permitted_request(probe, 'http://127.0.0.1/ui.js', 'GET', 'script'))
        for url, method, kind in (
            ('http://external.example/', 'GET', 'script'),
            ('http://127.0.0.1/login', 'POST', 'xhr'),
            ('http://127.0.0.1/reboot', 'GET', 'document'),
            ('http://127.0.0.1/api?token=secret', 'GET', 'fetch'),
            ('http://127.0.0.1/a', 'GET', 'image'),
            ('file:///etc/passwd', 'GET', 'document'),
            ('http://127.0.0.1:8000/', 'GET', 'document'),
        ):
            self.assertFalse(permitted_request(probe, url, method, kind))

    def test_nmap_filters_only_probed_non_web_services(self):
        xml = '''<host><address addr="192.168.2.1" addrtype="ipv4"/><ports>
        <port protocol="tcp" portid="22"><state state="open"/><service name="http" method="probed" conf="10"/></port>
        <port protocol="tcp" portid="445"><state state="open"/><service name="microsoft-ds" method="probed" conf="10"/></port>
        <port protocol="tcp" portid="631"><state state="open"/><service name="ipp" method="probed" conf="10"/></port>
        <port protocol="tcp" portid="23"><state state="open"/><service name="ssh" method="table" conf="3"/></port>
        </ports></host>'''
        self.assertEqual(parse_host(xml, ['192.168.2.0/24'], filter_services=True), [('192.168.2.1', 22), ('192.168.2.1', 631), ('192.168.2.1', 23)])
        self.assertEqual(len(parse_host(xml, ['192.168.2.0/24'])), 4)

    def test_provider_failure_keeps_queue_across_process_restart(self):
        with TemporaryDirectory() as directory, fixture() as url:
            urls = [url, 'http://127.0.0.1:9876/']
            job = JobManager(Path(directory))
            job.start(Settings(seed_urls=urls), '', model=ScriptedModel(mode='unavailable'))
            job.thread.join(5)
            self.assertFalse(job.running)
            self.assertEqual(job.snapshot().pending_urls, urls)
            replacement = JobManager(Path(directory))
            pending = replacement.store.read()['run']['pending_urls']
            replacement.start(Settings(seed_urls=pending), '', model=ScriptedModel())
            replacement.thread.join(5)
            self.assertFalse(replacement.running)
            self.assertEqual(replacement.snapshot().status, 'completed')
            self.assertEqual(replacement.snapshot().pending_urls, [])

    def test_endpoint_cap_retains_overflow_for_resume(self):
        with TemporaryDirectory() as directory, fixture() as url:
            urls = [url, 'http://127.0.0.1:9876/']
            job = JobManager(Path(directory))
            job.start(Settings(seed_urls=urls, max_endpoints=1), '', model=ScriptedModel())
            job.thread.join(5)
            self.assertEqual(job.snapshot().pending_urls, urls[1:])
            self.assertEqual(job.snapshot().added, 1)


if __name__ == '__main__':
    unittest.main()
