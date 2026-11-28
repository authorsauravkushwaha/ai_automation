"""Opt-in actual Chromium test, all servers/data local and synthetic.

Prepare Playwright + Chromium separately. Then set HINATA_REAL_BROWSER_ACCEPTANCE=1.
This test never downloads a browser or accesses a public website.
"""
from __future__ import annotations

import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hinata.security.browser_egress import guarded_page


@unittest.skipUnless(os.environ.get("HINATA_REAL_BROWSER_ACCEPTANCE") == "1", "actual Chromium acceptance is opt-in")
class RealGuardedBrowserTests(unittest.TestCase):
    def test_same_origin_post_and_cross_origin_redirect_subresource_isolation(self):
        from playwright.sync_api import sync_playwright

        forbidden_hits = []
        posted = []
        class OtherOrigin(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                forbidden_hits.append(self.path)
                self.send_response(200)
                self.end_headers()
            def log_message(self, *_args):
                pass
        other = ThreadingHTTPServer(("127.0.0.1", 0), OtherOrigin)
        other_url = f"http://127.0.0.1:{other.server_port}"
        class MainOrigin(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", other_url + "/redirected")
                    self.end_headers()
                    return
                html = (f'<html><body><img src="{other_url}/pixel">'
                        '<img src="/redirect"><form action="/submit" method="post">'
                        '<input id="field" name="field"><button type="submit">Send</button></form></body></html>')
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode())
            def do_POST(self):  # noqa: N802
                posted.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body id='receipt'>received</body></html>")
            def log_message(self, *_args):
                pass
        main = ThreadingHTTPServer(("127.0.0.1", 0), MainOrigin)
        threads = []
        for server in (main, other):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        try:
            url = f"http://127.0.0.1:{main.server_port}"
            with sync_playwright() as playwright, guarded_page(playwright, url, headless=True,
                    executable_path=os.environ.get("HINATA_TEST_CHROMIUM_EXECUTABLE")) as (page, policy):
                page.goto(url, wait_until="networkidle", timeout=15000)
                page.locator("#field").fill("synthetic-value")
                page.locator("button").click()
                page.locator("#receipt").wait_for(timeout=10000)
                self.assertGreaterEqual(policy.blocked, 2)
            self.assertEqual(posted, [b"field=synthetic-value"])
            self.assertEqual(forbidden_hits, [])
        finally:
            for server in (main, other):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
