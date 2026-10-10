from __future__ import annotations

import os
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from scout.api import create_server
from scout.boss_interface import AurenBossV2Interface, origin_for_url
from scout.browser_operator import V2BrowserController
from scout.discovery import MockSearchProvider
from scout.service import ScoutService
from scout.storage import V2StateStore
from scout.browser_operator import BrowserOperatorExecutor


CDP_V2 = "http://127.0.0.1:9223"
BOSS_SECRET = "live-test-only-shared-secret-0123456789"
_LIVE_ENABLED = (
    os.getenv("SCOUT_BROWSER_LIVE_TEST") == "1"
    and os.getenv("SCOUT_CDP_ENDPOINT", CDP_V2) == CDP_V2
)
_EXTERNAL_LIVE_ENABLED = (
    _LIVE_ENABLED
    and os.getenv("SCOUT_LIVE_RESEARCH_AUTHORIZED") == "1"
    and os.getenv("SCOUT_LIVE_RESEARCH_ALLOW_CREDIT_CONSUMPTION") == "1"
    and bool(os.getenv("SCOUT_LIVE_RESEARCH_URL", "").strip())
    and bool(os.getenv("SCOUT_LIVE_RESEARCH_QUERY", "").strip())
)


class ControlledPageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Controlled directory</title></head>
<body>
  <form id="search-form"><label for="query">Search directory</label>
    <input id="query" name="query" type="search" aria-label="Search directory">
    <button type="submit">Search</button>
  </form>
  <main><section id="results" aria-live="polite"></section>
    <table aria-label="Business table"><thead><tr><th>Name</th><th>City</th></tr></thead>
      <tbody><tr><td>Controlled Cafe</td><td>Test City</td></tr></tbody></table>
    <ul aria-label="Business list"><li>Controlled Cafe</li><li>Second controlled listing</li></ul>
    <p id="clipboard-source">Clipboard source text</p>
    <label for="notes">Notes</label><textarea id="notes"></textarea>
    <div style="height: 1800px" aria-hidden="true"></div>
  </main>
<script>
  const records = [
    '<article><h2>Harbor Cafe</h2><p>Harbor Cafe, Test City</p><a href="/company/harbor">Company profile</a></article>',
    '<article><h2>Garden Coffee</h2><p>Garden Coffee, Test City</p><a href="/company/garden">Company profile</a></article>'
  ];
  let currentPage = 0;
  function renderPage(page) {
    currentPage = page;
    const results = document.querySelector('#results');
    results.innerHTML = records[page];
    const oldButton = document.querySelector('#next-page');
    if (oldButton) oldButton.remove();
    if (page === 0) {
      const button = document.createElement('button');
      button.id = 'next-page';
      button.textContent = 'Next page';
      button.addEventListener('click', () => renderPage(1));
      results.append(button);
    }
  }
  document.querySelector('#search-form').addEventListener('submit', event => {
    event.preventDefault();
    renderPage(0);
  });
</script>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


@unittest.skipUnless(
    _LIVE_ENABLED,
    "set SCOUT_BROWSER_LIVE_TEST=1 with SCOUT_CDP_ENDPOINT=http://127.0.0.1:9223 to run Edge V2 integration tests",
)
class LiveV2BrowserOperatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_local_origin_opt_in = os.environ.get("SCOUT_ALLOW_LOCAL_TEST_ORIGINS")
        os.environ["SCOUT_ALLOW_LOCAL_TEST_ORIGINS"] = "1"
        cls.page_server = ThreadingHTTPServer(("127.0.0.1", 0), ControlledPageHandler)
        cls.page_thread = threading.Thread(target=cls.page_server.serve_forever, daemon=True)
        cls.page_thread.start()
        cls.page_url = f"http://127.0.0.1:{cls.page_server.server_address[1]}/directory"
        cls.origin = origin_for_url(cls.page_url)
        cls.temporary_directory = TemporaryDirectory(prefix="scout-edge-v2-live-")
        state_store = V2StateStore(Path(cls.temporary_directory.name) / "state.json")
        operator_executor = BrowserOperatorExecutor(state_store=state_store)
        cls.api_server = create_server(
            ScoutService(MockSearchProvider()),
            port=0,
            operator_executor=operator_executor,
            boss_shared_secret=BOSS_SECRET,
        )
        cls.api_thread = threading.Thread(target=cls.api_server.serve_forever, daemon=True)
        cls.api_thread.start()
        cls.boss = AurenBossV2Interface(
            BOSS_SECRET,
            f"http://127.0.0.1:{cls.api_server.server_address[1]}",
        )

    @classmethod
    def tearDownClass(cls):
        cls.api_server.shutdown()
        cls.api_server.server_close()
        cls.api_thread.join()
        cls.page_server.shutdown()
        cls.page_server.server_close()
        cls.page_thread.join()
        cls.temporary_directory.cleanup()
        if cls.previous_local_origin_opt_in is None:
            os.environ.pop("SCOUT_ALLOW_LOCAL_TEST_ORIGINS", None)
        else:
            os.environ["SCOUT_ALLOW_LOCAL_TEST_ORIGINS"] = cls.previous_local_origin_opt_in

    def test_boss_python_client_operates_real_edge_on_controlled_pages(self):
        controller = V2BrowserController(CDP_V2)
        controller.connect()
        try:
            controller.open_new_tab(self.page_url)
            context = controller._require_browser().contexts[0]
            context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin=self.origin,
            )
        finally:
            controller.disconnect()

        research_task_id = str(uuid.uuid4())
        research = self.boss.submit_task({
            "task_id": research_task_id,
            "goal": "Pesquisar cafeterias na página local controlada",
            "query": "coffee shops",
            "quantity": 2,
            "niche": "cafes",
            "locality": "Test City",
            "fields_of_interest": ["company_name", "source_url"],
            "start_url": self.page_url,
            "authorization": {
                "allowed_origins": [self.origin],
                "allow_credit_consumption": True,
            },
        })
        self.assertEqual(research["status"], "completed", research.get("error"))
        self.assertEqual([lead["company_name"] for lead in research["leads"]], [
            "Harbor Cafe", "Garden Coffee",
        ])
        self.assertTrue(all(lead["source_url"].startswith(self.origin) for lead in research["leads"]))

        task_id = str(uuid.uuid4())
        result = self.boss.submit_task({
            "task_id": task_id,
            "goal": "Validar leitura e clipboard na página local controlada",
            "authorization": {
                "allowed_origins": [self.origin],
                "allow_external_effects": True,
            },
            "actions": [
                {"action": "read_table", "target": {"role": "table", "accessible_name": "Business table"}},
                {"action": "read_list", "target": {"role": "list", "accessible_name": "Business list"}},
                {"action": "copy_text", "target": {"text": "Clipboard source text"}},
                {"action": "paste_text", "target": {"label": "Notes"}, "text": "Pasted through Edge"},
                {"action": "scroll", "delta_y": 500},
            ],
        })
        stored = self.boss.get_task(task_id)
        package_id = result["result_package"]["package_id"]
        package = self.boss.get_result(package_id)

        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(stored["task_id"], task_id)
        self.assertEqual(result["operations"][0]["result"]["rows"][0]["Name"], "Controlled Cafe")
        self.assertEqual(result["operations"][1]["result"]["items"][0]["text"], "Controlled Cafe")
        self.assertEqual(result["operations"][2]["result"]["text"], "Clipboard source text")
        self.assertTrue(result["operations"][3]["result"]["pasted"])
        self.assertEqual(package["package_id"], package_id)
        self.assertTrue(package["operator_result"]["evidence"])

        cleanup = V2BrowserController(CDP_V2)
        cleanup.connect()
        try:
            cleanup.close_tab(url=self.page_url)
        finally:
            cleanup.disconnect()

    @unittest.skipUnless(
        _EXTERNAL_LIVE_ENABLED,
        "set an explicitly authorized URL/query and SCOUT_LIVE_RESEARCH_ALLOW_CREDIT_CONSUMPTION=1 for public live research",
    )
    def test_authorized_public_source_search_uses_observed_results(self):
        url = os.environ["SCOUT_LIVE_RESEARCH_URL"].strip()
        query = os.environ["SCOUT_LIVE_RESEARCH_QUERY"].strip()
        origin = origin_for_url(url)
        result = self.boss.submit_task({
            "task_id": str(uuid.uuid4()),
            "goal": "Pesquisa pública explicitamente autorizada",
            "query": query,
            "quantity": 3,
            "start_url": url,
            "authorization": {
                "allowed_origins": [origin],
                "allow_credit_consumption": True,
            },
        })
        self.assertIn(result["status"], {"completed", "partial"}, result.get("error"))
        self.assertTrue(result.get("result_package"))
        self.assertTrue(all(lead.get("source_url") for lead in result.get("leads", [])))


if __name__ == "__main__":
    unittest.main()
