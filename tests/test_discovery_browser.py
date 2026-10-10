"""Opt-in real Chromium fixture test; no API key or external target is used."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from threading import Thread
import unittest

from test_discovery import make_probe, ScriptedModel
from discovery.agent import identify


@unittest.skipUnless(os.getenv('GLASSSCOUT_BROWSER_TEST') == '1', 'Optional Chromium integration test')
class BrowserIntegrationTests(unittest.TestCase):
    def test_rendered_router_verified_without_post_or_action_requests(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(('GET', self.path))
                if self.path == '/':
                    body = b'<html><head></head><body><script src="/ui.js"></script></body></html>'
                    content_type = 'text/html'
                elif self.path == '/ui.js':
                    body = b'''document.title = 'ASUS Router';
                    document.body.append('ASUSWRT administration');
                    fetch('/write', {method:'POST', body:'do not send'}).catch(()=>{});
                    fetch('/reboot').catch(()=>{});
                    fetch('/api?token=secret').catch(()=>{});
                    fetch('http://example.invalid/').catch(()=>{});
                    new WebSocket('ws://' + location.host + '/socket');'''
                    content_type = 'application/javascript'
                else:
                    body, content_type = b'Unexpected request', 'text/plain'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                requests.append(('POST', self.path))
                self.send_response(500)
                self.end_headers()
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            probe = make_probe(f'http://127.0.0.1:{server.server_port}/', browser_fallback=True,
                               service_seconds=45)
            events = []
            model = ScriptedModel(mode='unavailable')
            result = asyncio.run(identify(probe, model, lambda *args: events.append(args)))
            self.assertEqual(result.state, 'verified', (result.reason, events))
            self.assertTrue(any(o.source == 'browser' for o in result.observations))
            self.assertEqual(model.histories, [])
            self.assertTrue(requests)
            self.assertTrue(all(method == 'GET' and path in {'/', '/ui.js'} for method, path in requests), requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
