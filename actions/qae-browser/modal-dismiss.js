// The modal-dismiss rule (ui-audit-page.js's header lists every rule): a modal a person cannot close by pressing Escape,
// or, unless it is an alertdialog (where an explicit choice is the point), by a click on its backdrop outside it.
// modal-page.js finds the modals and the backdrop point; this file judges them.
//
// Judging a modal means pressing keys and clicking, which would move the explorer's walk, so it is never done in the
// explorer's tab. ui-audit.js hands `replay` the explorer's page when a modal is open there, with the control the explorer
// pressed before it appeared (modal-page.js's watchOpeners notes it), and `replay` opens the modal again in a window of
// its own, as recordWide does for a wide layout: a new browser context, signed in from the explorer's storage state, at
// the explorer's window size, never in its tab list, closed after. It loads the explorer's address. A modal open on load is
// judged as it is; else it presses the noted control (the one control of that tag and name, else of that CSS path; never a
// submit button) and waits for a modal. Once one shows it waits half a second for it to settle, as a person does, and
// presses Escape. Then, in a second window opened the same way, it clicks a point of the backdrop at least 24px outside
// the dialog. A modal holding edited input is not judged, nor the click half when no such point exists; when no modal
// opens, nothing is recorded.
//
// The window shares the explorer's sign-in with the app's real server, so it can only read: every request other than a GET
// or HEAD is aborted, service workers are blocked, and no websocket reaches the server. It types nothing, and presses only
// the noted control, Escape and the backdrop point.
const fs = require("fs");
const path = require("path");

const read = name => fs.readFileSync(path.join(__dirname, name), "utf8");
const source = ["ui-audit-page.js", "name-page.js", "modal-page.js"].map(read).join("\n");
const openScript = `(() => {\n${source}\nreturn openModals();\n})()`;
// The explorer's tab's init script: it notes each press and reports a dialog appearing, and changes nothing.
const watchScript = `(() => {\n${source}\nwatchOpeners();\n})()`;
const findScript = opener => `(() => {\n${source}\nreturn findOpener(${JSON.stringify(opener)});\n})()`;

const BUDGET = 20000;   // ms both halves of one replay may take
const IDLE = 3000;      // ms the load's network may take to go quiet, as for a table its data fills late
const SETTLE = 500;     // ms after that, as recordWide waits
const APPEAR = 3000;    // ms a modal has to appear, after the load or the press that opens it
const CLOSE = 1000;     // ms a modal has to close after Escape or the click, its exit animation included
const POLL = 100;
const READ = /^(GET|HEAD)$/;
const ESCAPE = "pressing Escape leaves the dialog open", CLICK = "a click on the backdrop outside it leaves the dialog open";

const left = run => Math.max(1, run.until - Date.now());
const same = (a, b) => a.at === b.at && a.name === b.name;

// Whether a modal is open in `page`: the explorer's tab, read and never changed.
async function anyOpen(page) {
  try {
    return (await page.evaluate(openScript)).length > 0;
  } catch {
    return false;
  }
}

// The modal opened last in `page`, within `wait` ms, or null.
async function appear(page, wait, run) {
  const end = Math.min(Date.now() + wait, run.until);
  for (;;) {
    const last = (await page.evaluate(openScript)).pop();
    if (last || Date.now() >= end) return last || null;
    await page.waitForTimeout(POLL);
  }
}

// Whether `modal` is still open in `page` CLOSE ms later.
async function stays(page, modal, run) {
  const end = Math.min(Date.now() + CLOSE, run.until);
  for (;;) {
    if (!(await page.evaluate(openScript)).some(open => same(open, modal))) return false;
    if (Date.now() >= end) return true;
    await page.waitForTimeout(POLL);
  }
}

// Loads the address and returns the modal that shows, or null. The first half settles how it opens (run.onLoad): one
// open on load, unless the page's load was judged before, else the one the noted control opens. The second opens it
// the same way.
async function shows(page, run) {
  await page.goto(run.url, { waitUntil: "load", timeout: left(run) });
  await page.waitForLoadState("networkidle", { timeout: Math.min(IDLE, left(run)) }).catch(() => {});
  await page.waitForTimeout(SETTLE);
  if (run.onLoad !== false) {
    const onLoad = await appear(page, run.opener && run.onLoad === undefined ? 0 : APPEAR, run);
    if (run.onLoad === undefined) run.onLoad = Boolean(onLoad);
    if (run.onLoad) return run.loadJudged ? null : onLoad;
  }
  if (!run.opener) return null;
  const opener = (await page.evaluateHandle(findScript(run.opener))).asElement();
  if (!opener) return null;
  await opener.click({ timeout: Math.min(APPEAR, left(run)) });
  return appear(page, APPEAR, run);
}

// The modal to judge, read again once it has settled. A person waits to see a modal before pressing a key, and some
// ignore both while their entrance animation runs (and only then take the focus Escape needs).
async function open(page, run) {
  if (!(await shows(page, run))) return null;
  await page.waitForTimeout(SETTLE);
  return appear(page, 0, run);
}

// One half: in a new window of the hook's own, opens the modal, hands it to `act`, and returns {modal, stays}, or null
// when no modal opened, it holds edited input, or `act` found nothing to do.
async function half(run, act) {
  const context = await run.browser.newContext({ viewport: run.viewport, storageState: run.storage, serviceWorkers: "block" });
  try {
    await context.route("**/*", route => (READ.test(route.request().method()) ? route.continue() : route.abort()));
    await context.routeWebSocket(/.*/, () => {});   // never connected to the server
    const page = await context.newPage();
    const modal = await open(page, run);
    if (!modal || modal.edited || !(await act(page, modal))) return null;
    return { modal, stays: await stays(page, modal, run) };
  } finally {
    await context.close().catch(() => {});
  }
}

// Replays the modal open in the explorer's `page` and returns {at, text, detail} when it stays open after Escape or a
// backdrop click, else null. `opener` is the control the explorer pressed before it appeared, or null; `loadJudged` says
// a modal open on this page's load was judged before. Throws only what the explorer's page throws.
async function replay(page, url, opener, loadJudged) {
  const browser = page.context().browser();
  if (!browser || !/^(https?|file):/.test(url)) return null;
  const run = { browser, url, opener, loadJudged, viewport: page.viewportSize(), storage: await page.context().storageState(),
    until: Date.now() + BUDGET };
  const escape = await half(run, async (hidden) => { await hidden.keyboard.press("Escape"); return true; }).catch(() => null);
  if (!escape) return null;
  const click = escape.modal.alert ? null : await half(run, async (hidden, modal) => {
    if (!same(modal, escape.modal) || !modal.point) return false;
    await hidden.mouse.click(modal.point.x, modal.point.y);
    return true;
  }).catch(() => null);
  const stuck = [escape.stays, Boolean(click && click.stays)];
  if (!stuck.some(Boolean)) return null;
  const how = run.onLoad ? "open on load" : `opened by pressing "${opener.name || opener.at}"`;
  const detail = stuck.every(Boolean) ? `${ESCAPE}, and so does a click on the backdrop outside it` : stuck[0] ? ESCAPE : CLICK;
  return { at: escape.modal.at, text: escape.modal.name, detail: `${detail} (${how})` };
}

module.exports = { anyOpen, replay, openScript, watchScript };
