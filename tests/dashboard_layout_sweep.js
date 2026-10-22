// Run by tests/test_dashboard_layout_browser.py: opens the dashboard at argv[2] in a real Chromium and,
// at each window width in argv[3] (comma separated), prints one JSON line: the cells of a .pr-row that
// end outside their row, how many rows show an idle line, a timeline and a meter, and how far the page, the view and each pull-request list scroll sideways.
// The browser comes from the Playwright in VV_PLAYWRIGHT_DIR (its node_modules), channel VV_BROWSER_CHANNEL.
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
  await page.goto(process.argv[2], { waitUntil: "networkidle" });
  await page.waitForSelector(".pr-row");
  for (const width of process.argv[3].split(",").map(Number)) {
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
