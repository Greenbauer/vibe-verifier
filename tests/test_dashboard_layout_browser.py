"""The Pull requests view in a real Chromium, at every window width from a phone to a wide desktop: no row cell
ends outside its row, and neither the page, the view nor a pull-request list scrolls sideways. The rows are
the hard ones: a long title, author and head; a CI timeline with its idle line; the check meter with one; no
checks at all. tests/test_dashboard_pr_row_fit.py adds up the grid's minimums from the CSS; this one renders
it, so a change anywhere (a wider cell, a new line, a breakpoint) shows. A 921 to 1060px window used to push
the CI column and the arrow out of the row, and nothing caught it: the other dashboard tests have no layout.

It needs a Playwright with a browser: VV_PLAYWRIGHT_DIR names the directory whose node_modules holds it (the
catalog's QA browser tests job passes the qae-browser action's), VV_BROWSER_CHANNEL the channel ("chromium"
by default; "chrome" uses an installed Google Chrome). Without VV_PLAYWRIGHT_DIR it skips, so that job checks
the directory first: a skip there must not read as a pass."""
import json
import os
import subprocess
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import ROOT

STATIC = ROOT / "dashboard" / "static"
# Run with `node -e`: opens the dashboard at argv[1] in a real Chromium and, at each window width in argv[2]
# (comma separated), prints one JSON line: the cells of a .pr-row that end outside their row, how many rows
# show an idle line, a timeline and a meter, and how far the page, the view and each list scroll sideways.
# The browser comes from the Playwright in VV_PLAYWRIGHT_DIR (its node_modules), channel VV_BROWSER_CHANNEL.
SWEEP = r"""
"use strict";
const path = require("node:path");

const modules = path.join(process.env.VV_PLAYWRIGHT_DIR, "node_modules");
const load = name => { try { return require(path.join(modules, name)); } catch { return null; } };
const { chromium } = load("playwright") || load("playwright-core");

(async () => {
  const browser = await chromium.launch({ channel: process.env.VV_BROWSER_CHANNEL || "chromium" });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on("pageerror", error => errors.push(String(error)));
  await page.goto(process.argv[1], { waitUntil: "networkidle" });
  await page.waitForSelector(".pr-row");
  for (const width of process.argv[2].split(",").map(Number)) {
    await page.setViewportSize({ width, height: 900 });
    const measured = await page.evaluate(() => {
      const outside = [];
      for (const row of document.querySelectorAll(".pr-row")) {
        const box = row.getBoundingClientRect();
        for (const cell of row.children) {
          const own = cell.getBoundingClientRect();
          if (own.width === 0 && own.height === 0) continue;
          const past = Math.max(own.right - box.right, box.left - own.left);
          if (past > 0.5) outside.push(`${row.querySelector(".pr-title, b")?.textContent.slice(0, 30)}: ${cell.className} ${past.toFixed(1)}px`);
        }
      }
      const sideways = node => node.scrollWidth - node.clientWidth;
      const count = selector => document.querySelectorAll(selector).length;
      return { rows: count(".pr-row"), idle: count(".pr-row .idle-line"), timelines: count(".pr-row .timeline-cell"),
        meters: count(".pr-row .check-meter"), outside, page: sideways(document.scrollingElement),
        view: sideways(document.querySelector("main")), lists: Math.max(...[...document.querySelectorAll(".pr-list")].map(sideways)) };
    });
    console.log(JSON.stringify({ width, ...measured }));
  }
  console.log(JSON.stringify({ errors }));
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
"""
# Every 25px from a small phone to a wide desktop, and both sides of each breakpoint the row grid or the
# sidebar changes at (styles.css: 650 phone, 920 sidebar above the list, 1080 the row's wide minimums).
WIDTHS = sorted(set(range(320, 1601, 25)) | {650, 651, 920, 921, 1080, 1081, 1100})
TYPES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml", ".png": "image/png"}


def snapshot():
    """A dashboard payload whose rows stretch each column: times are relative to now so the idle lines show."""
    now = datetime.now(timezone.utc)
    at = lambda minutes: (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    def pull(number, **extra):
        return {"repository": "octocat/a-repository-with-a-rather-long-name", "number": number, "title": f"Change {number}",
                "author": "octocat", "head_sha": "abcdef1234567890", "created_at": at(60 * 24 * 40),
                "html_url": f"https://github.com/octocat/a-repository-with-a-rather-long-name/pull/{number}",
                "statuses": [], "expected": [], "runs": [], "checks": [], **extra}
    def check(id_, name, category, start, end):
        return {"id": id_, "name": name, "category": category, "status": "completed", "started_at": at(start), "completed_at": at(end)}
    def job(id_, name, start, end):
        return {"id": id_, "name": name, "created_at": at(start), "started_at": at(start), "completed_at": at(end), "steps": []}
    names = ["acceptance criteria walk on the preview", "browser suite shard 1 of 4", "lint, build and verify", "unit"]
    timeline = pull(
        101, title="fix(dashboard): a very long pull request title that keeps going well past the width of its column",
        author="a-contributor-with-a-long-login", behind_by=12, comments=5,
        checks=[check(i, name, "failed" if i == 2 else "success", 90, 80 - 10 * i) for i, name in enumerate(names, 1)],
        runs=[{"id": 9, "name": "CI", "jobs": [job(i, name, 90, 80 - 10 * i) for i, name in enumerate(names, 1)]}])
    meter = pull(102, title="A meter row: commit statuses draw no timeline; finished an hour ago, so it has an idle line",
                 statuses=[check(20 + i, name, "success", 70, 60) for i, name in enumerate(names)])
    running = pull(103, title="Checks still running", checks=[{"id": 30, "name": "unit", "category": "pending", "status": "queued"},
                                                              check(31, "lint", "failed", 20, 10)])
    bare = pull(104, title="No checks reported yet")
    return {"version": 1, "owner": "octocat", "github": {
        "owner": "octocat", "sampled_at": at(0), "coverage": {"selected": 1},
        "repositories": [{"repository": "octocat/a-repository-with-a-rather-long-name", "pulls": [timeline, meter, running, bare]}]}}


def serve(payload):
    """The page, its assets under /assets/ and the payload at /api/dashboard, on a free local port."""
    body = json.dumps(payload).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/api/dashboard":
                return self.reply(body, "application/json")
            if path.startswith("/assets/"):
                asset = STATIC / path[len("/assets/"):]
                if asset.is_file() and asset.resolve().parent == STATIC.resolve():
                    return self.reply(asset.read_bytes(), TYPES.get(asset.suffix, "application/octet-stream"))
                self.send_response(404)
                self.end_headers()
                return None
            return self.reply((STATIC / "index.html").read_bytes(), "text/html")

        def reply(self, data, kind):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@unittest.skipUnless(os.environ.get("VV_PLAYWRIGHT_DIR"), "needs VV_PLAYWRIGHT_DIR (a Playwright with a browser)")
class DashboardLayoutInABrowser(unittest.TestCase):
    def test_no_row_cell_ends_outside_its_row_and_nothing_scrolls_sideways_at_any_width(self):
        server = serve(snapshot())
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        result = subprocess.run(
            ["node", "-e", SWEEP, f"http://127.0.0.1:{server.server_address[1]}/", ",".join(map(str, WIDTHS))],
            capture_output=True, text=True, timeout=300, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(lines[-1], {"errors": []})
        widths = lines[:-1]
        self.assertEqual([line["width"] for line in widths], WIDTHS)
        problems = [f"{line['width']}px: {line['outside']} page {line['page']} view {line['view']} list {line['lists']}"
                    for line in widths if line["outside"] or line["page"] > 0 or line["view"] > 0 or line["lists"] > 0]
        # The fixture still stretches what it means to: four rows, two idle lines, two timelines (one still running) and a meter.
        self.assertEqual({(line["rows"], line["idle"], line["timelines"], line["meters"]) for line in widths}, {(4, 2, 2, 1)})
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
