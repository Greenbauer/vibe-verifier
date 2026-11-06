// The QAE harness's layout and copy records, and the same audit from a shell.
//
// As playwright-mcp's --init-page hook it runs the layout audit (ui-audit-page.js) on every page
// the explorer opens: a second after each load and each navigation of the tab, half a second after a
// dialog, alert or status message appears, and at once when a button gains a spinner, so a state the
// explorer only passes through is measured too. Each finding is appended once per page, viewport
// width, rule and element to ui-audit.jsonl in the run's output directory, which the ui-audit gate
// reads:
//
//     {"page": "<URL, query and fragment dropped>", "width": 1280, "rule": "touching",
//      "at": "<CSS path>", "text": "...", "other": "...", "detail": "..."}
//
// At the same moments it records the page's copy (copy-page.js): each sentence and label a person can
// read, once per page and text, to copy.jsonl, which the copy-audit grader grades and the copy-audit
// gate judges:
//
//     {"page": "...", "width": 1280, "kind": "sentence", "text": "...", "at": "<CSS path>"}
//
// The explorer never sees it and cannot change it: the hook runs in the harness's Node process.
//
// A table that scrolls while the layout has room for it (table-scroll) shows only where there is room, often only in
// a wide window, which the explorer may never use. So once per page, when the explorer's window is narrower than
// WIDE, the hook also loads the page in a window of its own at WIDE pixels, signed in as the explorer is (a new
// context in the same browser, from the explorer's storage state, closed after), and records that rule's findings
// at that width. The explorer's tab list never shows it. It is a second load of the same address, as a reload is.
//
// From a shell, before a push, on any page a browser can reach:
//
//     node actions/qae-browser/ui-audit.js [--width N]... [--storage-state FILE] [--channel chrome] [--copy FILE] URL...
//
// --width defaults to 1280 and 375. --storage-state signs the browser in (Playwright's storage
// state file, as `npx playwright codegen --save-storage` writes it). --channel chrome uses the
// installed Google Chrome instead of the Playwright build's chromium. --copy FILE also writes the
// pages' copy record there, for `bin/vibe-verifier copy-audit`. It prints one line per finding and
// exits 1 when there is one, 0 when there is none, 2 when a page cannot be opened.
const fs = require("fs");
const path = require("path");
// The audit as one script for page.evaluate: the file's functions inside a closure, so none of them
// lands on the page's window, and the closure's value is the findings.
const read = name => fs.readFileSync(path.join(__dirname, name), "utf8");
const script = `(() => {\n${read("ui-audit-page.js")}\n${read("glyph-page.js")}\n${read("name-page.js")}\n${read("wrap-page.js")}\n${read("scroll-page.js")}\n${read("gutter-page.js")}\nreturn audit();\n})()`;
// The copy record's finder, with the audit's helpers it uses (visible, cssPath) in the same closure.
const copyScript = `(() => {\n${read("ui-audit-page.js")}\n${read("copy-page.js")}\nreturn copyBlocks();\n})()`;

const RECORD = "ui-audit.jsonl";
const WIDE = 1920;   // px: a common desktop window, where a capped layout leaves room beside its tables
const WIDE_RULES = ["table-scroll"];   // the rules whose defect needs room to show
const COPY = "copy.jsonl";

function pageOf(url) {
  try {
    const parsed = new URL(url);
    return (parsed.origin === "null" ? parsed.protocol + "//" : parsed.origin) + parsed.pathname;
  } catch {
    return url;
  }
}

// Runs the audit on the page and hands each finding not written before to `write`.
async function record(page, write, written) {
  let found;
  try {
    found = await page.evaluate(script);
  } catch {
    return;   // the tab navigated or closed mid-measure; the next load measures again
  }
  const where = pageOf(page.url()), width = (page.viewportSize() || {}).width || null;
  for (const finding of found) {
    const key = JSON.stringify([where, width, finding.rule, finding.at]);
    if (written.has(key)) continue;
    written.add(key);
    write({ page: where, width, ...finding });
  }
}

// Loads the page once in a window of its own at WIDE, signed in as the explorer's context is, and hands each
// WIDE_RULES finding not written before to `write`. Nothing when the explorer's window is that wide already, when
// the page was measured wide before, or when the browser gives no second context. The address is read once: the
// explorer may move on while the window opens, and what is loaded must be what the finding names.
async function recordWide(page, write, written, measured) {
  let context;
  try {   // never the explorer's problem: anything that fails here leaves its own measures as they were
    const url = page.url(), where = pageOf(url), width = (page.viewportSize() || {}).width || 0;
    const browser = page.context().browser();
    if (width >= WIDE || measured.has(where) || !browser || !/^(https?|file):/.test(url)) return;
    measured.add(where);
    context = await browser.newContext({ viewport: { width: WIDE, height: 1080 }, storageState: await page.context().storageState() });
    const wide = await context.newPage();
    await wide.goto(url, { waitUntil: "load", timeout: 20000 });
    await wide.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});   // a table its data fills late
    await wide.waitForTimeout(500);
    for (const finding of (await wide.evaluate(script)).filter(f => WIDE_RULES.includes(f.rule))) {
      const key = JSON.stringify([where, WIDE, finding.rule, finding.at]);
      if (written.has(key)) continue;
      written.add(key);
      write({ page: where, width: WIDE, ...finding });
    }
  } catch {
    // a page that will not load a second time, or a browser that cannot open a second context, keeps the
    // explorer's widths alone
  } finally {
    if (context) await context.close().catch(() => {});
  }
}

// Runs the copy finder on the page and hands each block of text not written for this page before to `write`.
async function recordCopy(page, write, written) {
  let found;
  try {
    found = await page.evaluate(copyScript);
  } catch {
    return;
  }
  const where = pageOf(page.url()), width = (page.viewportSize() || {}).width || null;
  for (const block of found) {
    const key = JSON.stringify([where, block.text]);
    if (written.has(key)) continue;
    written.add(key);
    write({ page: where, width, ...block });
  }
}

exports.default = async ({ page }) => {
  const { outputDir } = require("./console-pages.js");
  const dir = outputDir(process.argv);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, RECORD), copyFile = path.join(dir, COPY);
  // Made at once, empty: a run whose pages are clean still has a record, so the gate can tell clean from not loaded.
  fs.appendFileSync(file, "");
  fs.appendFileSync(copyFile, "");
  const write = line => fs.appendFileSync(file, JSON.stringify(line) + "\n");
  const writeCopy = line => fs.appendFileSync(copyFile, JSON.stringify(line) + "\n");
  const written = new Set(), copied = new Set(), measuredWide = new Set();
  let timer;
  const soon = ms => {
    clearTimeout(timer);
    timer = setTimeout(() => record(page, write, written).then(() => recordCopy(page, writeCopy, copied))
      .then(() => recordWide(page, write, written, measuredWide)), ms);
  };
  page.on("load", () => soon(1000));
  page.on("framenavigated", frame => { if (frame === page.mainFrame()) soon(1000); });
  page.on("close", () => clearTimeout(timer));
  await page.exposeBinding("__vibeVerifierMeasure", (source, ms) => soon(ms));
  await page.addInitScript(() => {
    const MESSAGE = "dialog, [role=dialog], [role=alertdialog], [role=alert], [role=status], [aria-live]";
    const spinning = node => node.nodeType === 1 && node.closest("button, [role=button]")
      && (/spin|loader|loading/i.test(node.getAttribute("class") || "") || node.getAttribute("role") === "progressbar");
    const message = node => {
      const el = node.nodeType === 1 ? node : node.parentElement;
      return el && (el.closest(MESSAGE) || (el.querySelector && el.querySelector(MESSAGE)));
    };
    new MutationObserver(changes => {
      const added = changes.flatMap(change => [...change.addedNodes]);
      if (added.some(spinning)) window.__vibeVerifierMeasure(0);
      else if (added.some(message) || changes.some(change => change.attributeName === "open")) window.__vibeVerifierMeasure(500);
    }).observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ["open"] });
  });
  soon(1000);
};
exports.record = record;
exports.recordCopy = recordCopy;
exports.recordWide = recordWide;
exports.pageOf = pageOf;
exports.script = script;
exports.copyScript = copyScript;

// The shell form. Playwright comes from the catalog's pinned browser toolchain (tools/qae-browser).
function parse(argv) {
  const options = { widths: [], urls: [] };
  for (let i = 0; i < argv.length; i++) {
    const flag = { "--width": "width", "--storage-state": "storageState", "--channel": "channel", "--copy": "copy" }[argv[i]];
    if (flag === "width") options.widths.push(Number(argv[++i]));
    else if (flag) options[flag] = argv[++i];
    else options.urls.push(argv[i]);
  }
  return options;
}

async function main(argv) {
  const { widths, urls, storageState, channel, copy } = parse(argv);
  if (!urls.length || widths.some(width => !(width > 0))) {
    console.error("usage: node actions/qae-browser/ui-audit.js [--width N]... [--storage-state FILE] [--channel chrome] [--copy FILE] URL...");
    return 2;
  }
  const catalog = path.join(__dirname, "..", "..");
  const tools = require("child_process").execFileSync("python3", [path.join(catalog, "bin", "vibe-verifier"), "tool", "qae-browser"], { encoding: "utf8" }).trim();
  const { chromium } = require(require.resolve("playwright", { paths: [tools] }));
  const browser = await chromium.launch(channel ? { channel } : {});
  let count = 0, status = 0;
  const copied = new Set();
  if (copy) fs.writeFileSync(copy, "");
  try {
    for (const width of widths.length ? widths : [1280, 375]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, storageState });
      const page = await context.newPage();
      for (const url of urls) {
        try {
          await page.goto(url, { waitUntil: "load", timeout: 60000 });
        } catch (error) {
          console.error(`${url}: ${error.message.split("\n")[0]}`);
          status = 2;
          continue;
        }
        await page.waitForTimeout(1500);
        await record(page, line => {
          count++;
          console.log(`${line.page} @${line.width}px ${line.rule}: ${line.text || line.at}${line.other ? ` / ${line.other}` : ""}: ${line.detail}`);
        }, new Set());
        if (copy) await recordCopy(page, line => fs.appendFileSync(copy, JSON.stringify(line) + "\n"), copied);
      }
      await context.close();
    }
  } finally {
    await browser.close();
  }
  console.error(`ui-audit: ${count} finding(s)`);
  return status || (count ? 1 : 0);
}

if (require.main === module) main(process.argv.slice(2)).then(code => process.exit(code), error => { console.error(error); process.exit(2); });
