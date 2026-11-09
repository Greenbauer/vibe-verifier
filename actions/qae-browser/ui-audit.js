// The QAE harness's layout and copy records, and the same audit from a shell.
//
// As playwright-mcp's --init-page hook it runs the layout audit (ui-audit-page.js) on every page
// the explorer opens: a second after each load and each navigation of the tab, half a second after a
// dialog, alert, status message or text field appears, 0.6 seconds after the explorer last typed into a
// field or ticked a box, and at once when a button gains a spinner, so a state the explorer only passes
// through is measured too. Each finding is appended once per page, viewport width, rule and element to
// ui-audit.jsonl in the run's output directory, which the ui-audit gate reads:
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
// An icon or spinner off its control's middle (icon-off-center) shows only in pixels: a CSS-drawn mark or a spinner
// tilted in 3D draws its ink away from the boxes the DOM reports. So at each measure the hook also screenshots up to
// ICONS controls in the window whose only content is one mark (icon-page.js finds them), three frames FRAME_GAP ms
// apart, and finds the ink in each (ink.js). Each control is sampled once per page, width and mark.
//
// The explorer never sees it and cannot change it: the hook runs in the harness's Node process.
//
// A table that scrolls while the layout has room for it (table-scroll) shows only where there is room, often only in
// a wide window, which the explorer may never use. So once per page, when the explorer's window is narrower than
// WIDE, the hook also loads the page in a window of its own at WIDE pixels, signed in as the explorer is (a new
// context in the same browser, from the explorer's storage state, closed after), and records that rule's findings
// at that width. The explorer's tab list never shows it. It is a second load of the same address, as a reload is.
//
// A modal a person cannot close by Escape or a backdrop click (modal-dismiss) shows only when keys are pressed, which would
// move the explorer's walk. So when a modal is open in the explorer's tab, the hook replays it in such windows of its own
// (modal-dismiss.js): once per page, width and control the explorer pressed to open it, at most MODALS per page and width,
// one replay at a time. The explorer's tab only gains an init script that notes what each press pressed.
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
const RULES = ["ui-audit-page.js", "row-page.js", "glyph-page.js", "name-page.js", "wrap-page.js", "scroll-page.js", "gutter-page.js", "field-page.js", "icon-page.js"];
const rules = RULES.map(read).join("\n");
const script = `(() => {\n${rules}\nreturn audit();\n})()`;
// The controls icon-off-center screenshots, found by the same rule files.
const iconScript = `(() => {\n${rules}\nreturn iconControls();\n})()`;
const { decodePng, inkOffset, offCenter } = require("./ink.js");
const modal = require("./modal-dismiss.js");
// The copy record's finder, with the audit's helpers it uses (visible, cssPath) in the same closure.
const copyScript = `(() => {\n${read("ui-audit-page.js")}\n${read("copy-page.js")}\nreturn copyBlocks();\n})()`;

const RECORD = "ui-audit.jsonl";
const WIDE = 1920;   // px: a common desktop window, where a capped layout leaves room beside its tables
const WIDE_RULES = ["table-scroll"];   // the rules whose defect needs room to show
const COPY = "copy.jsonl";
const ICONS = 4;          // controls icon-off-center screenshots per measure, each FRAMES times: the rest wait for the next
const FRAMES = 3;         // a spinner turns, so a control is judged off-center only when every frame is
const FRAME_GAP = 120;    // ms between frames
const MODALS = 3;         // modal replays per page and width

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

// Replays the modal open in the explorer's tab (modal-dismiss.js) while one is, one replay at a time, and hands a
// modal-dismiss finding not written before to `write`. Each replay is once per page, width and opener, the control the
// explorer pressed before a modal appeared (`state.pending`, from the tab's init script); with none it is the page's
// load. Never the explorer's problem: a failure here leaves its walk as it was.
async function recordModals(page, write, written, state) {
  if (state.busy) return;
  state.busy = true;
  try {
    while (await modal.anyOpen(page)) {
      const url = page.url(), where = pageOf(url), width = (page.viewportSize() || {}).width || null;
      const id = JSON.stringify([where, width]), done = state.pages.get(id) || { tried: new Set(), runs: 0 };
      state.pages.set(id, done);
      const opener = state.pending.shift() || null, key = opener ? JSON.stringify(opener) : "load";
      if (!done.tried.has(key) && done.runs < MODALS) {
        const loadJudged = done.tried.has("load");
        done.tried.add(key).add("load");   // every replay loads the page, so the first judges a modal open on load
        done.runs++;
        const found = await modal.replay(page, url, opener, loadJudged);
        const line = found && JSON.stringify([where, width, "modal-dismiss", found.at]);
        if (found && !written.has(line)) {
          written.add(line);
          write({ page: where, width, rule: "modal-dismiss", ...found });
        }
      }
      if (!state.pending.length) break;
    }
  } catch {
    // the tab navigated or closed, or the browser gave no second context
  } finally {
    state.pending.length = 0;
    state.busy = false;
  }
}

// Screenshots up to `most` controls whose only content is one mark, not sampled with that mark on this page at this width,
// FRAMES times, and hands an icon-off-center finding for each whose ink sat off its middle in every frame to `write`.
// A control that moved or went while it was sampled (the explorer scrolled, the spinner stopped) is left for a later
// measure. Never the explorer's problem: a failure here costs only this sample.
async function recordIcons(page, write, written, sampled, most = ICONS) {
  try {
    const where = pageOf(page.url()), width = (page.viewportSize() || {}).width || null;
    const id = control => JSON.stringify([where, width, control.at, control.mark]);
    const controls = (await page.evaluate(iconScript)).filter(control => !sampled.has(id(control))).slice(0, most);
    if (!controls.length) return;
    const samples = controls.map(() => []);
    for (let frame = 0; frame < FRAMES; frame++) {
      if (frame) await new Promise(resolve => setTimeout(resolve, FRAME_GAP));
      for (const [i, control] of controls.entries()) {
        samples[i].push(inkOffset(decodePng(await page.screenshot({ clip: control.clip, caret: "initial", timeout: 5000 })), control));
      }
    }
    const still = new Set((await page.evaluate(iconScript)).map(control => control.at + JSON.stringify(control.clip)));
    if (pageOf(page.url()) !== where) return;
    for (const [i, control] of controls.entries()) {
      if (!still.has(control.at + JSON.stringify(control.clip))) continue;
      sampled.add(id(control));
      const detail = offCenter(samples[i]), key = JSON.stringify([where, width, "icon-off-center", control.at]);
      if (!detail || written.has(key)) continue;
      written.add(key);
      write({ page: where, width, rule: "icon-off-center", at: control.at, text: control.text, detail });
    }
  } catch {
    // the tab navigated or closed mid-sample, or a screenshot failed: the next measure samples again
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
  const written = new Set(), copied = new Set(), measuredWide = new Set(), sampled = new Set();
  const modals = { pending: [], pages: new Map(), busy: false };
  let timer;
  const soon = ms => {
    clearTimeout(timer);
    timer = setTimeout(() => record(page, write, written).then(() => recordIcons(page, write, written, sampled))
      .then(() => recordCopy(page, writeCopy, copied)).then(() => recordWide(page, write, written, measuredWide))
      .then(() => recordModals(page, write, written, modals)), ms);
  };
  // The other widths the run asks for, captured after each of the explorer's screenshots (widths.js).
  require("./widths.js").install(page, dir, measured => record(measured, write, written));
  page.on("load", () => soon(1000));
  page.on("framenavigated", frame => { if (frame === page.mainFrame()) soon(1000); });
  page.on("close", () => clearTimeout(timer));
  await page.exposeBinding("__vibeVerifierMeasure", (source, ms) => soon(ms));
  await page.exposeBinding("__vibeVerifierModal", (source, opener) => {
    if (opener) modals.pending.push(opener);
    soon(500);
  });
  await page.addInitScript(modal.watchScript);
  await page.addInitScript(() => {
    const MESSAGE = "dialog, [role=dialog], [role=alertdialog], [role=alert], [role=status], [aria-live]";
    const FIELD = "input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea";
    const field = node => node.nodeType === 1 && (node.matches(FIELD) || Boolean(node.querySelector(FIELD)));
    const spinning = node => node.nodeType === 1 && node.closest("button, [role=button]")
      && (/spin|loader|loading/i.test(node.getAttribute("class") || "") || node.getAttribute("role") === "progressbar");
    const message = node => {
      const el = node.nodeType === 1 ? node : node.parentElement;
      return el && (el.closest(MESSAGE) || (el.querySelector && el.querySelector(MESSAGE)));
    };
    new MutationObserver(changes => {
      const added = changes.flatMap(change => [...change.addedNodes]);
      if (added.some(spinning)) window.__vibeVerifierMeasure(0);
      else if (added.some(message) || added.some(field) || changes.some(change => change.attributeName === "open")) window.__vibeVerifierMeasure(500);
    }).observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ["open"] });
    // What a person typed is in a field's value, which no mutation shows: measure once the typing stops. Ticking a box,
    // which can show a field by CSS alone, is an input too.
    let typing;
    const typed = () => { clearTimeout(typing); typing = setTimeout(() => window.__vibeVerifierMeasure(0), 600); };
    addEventListener("input", typed, true);
  });
  soon(1000);
};
exports.record = record;
exports.recordCopy = recordCopy;
exports.recordWide = recordWide;
exports.recordIcons = recordIcons;
exports.recordModals = recordModals;
exports.iconScript = iconScript;
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
        const print = line => {
          count++;
          console.log(`${line.page} @${line.width}px ${line.rule}: ${line.text || line.at}${line.other ? ` / ${line.other}` : ""}: ${line.detail}`);
        };
        const written = new Set();
        await record(page, print, written);
        await recordIcons(page, print, written, new Set(), Infinity);
        await recordModals(page, print, written, { pending: [], pages: new Map(), busy: false });
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
