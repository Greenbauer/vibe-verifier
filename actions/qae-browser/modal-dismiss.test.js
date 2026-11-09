// The modal-dismiss rule's own tests, with stand-ins for the browser: the hook's bookkeeping (ui-audit.js's
// recordModals) and the replay's guards (modal-dismiss.js). Run by tests/test_ui_audit.py; the rule in a real browser is
// tests/test_qae_ui_audit_modal.py.
const assert = require("node:assert");
const { test } = require("node:test");

const dismiss = require("./modal-dismiss.js");
const hook = require("./ui-audit.js");
const { replay: realReplay } = dismiss;   // the bookkeeping tests below swap dismiss.replay, which ui-audit.js calls

const state = () => ({ pending: [], pages: new Map(), busy: false });
const opener = name => ({ tag: "button", name, at: "main > button" });
// The explorer's tab at `url`, with a modal open while `open()` says so; `calls` lists each replay asked of `answer`.
function tab(url, open, answer = async () => null, width = 1280) {
  const calls = [];
  dismiss.replay = async (page, at, pressed, loadJudged) => {
    calls.push([pressed && pressed.name, loadJudged]);
    return answer(calls.length);
  };
  return { calls, page: { url: () => url, viewportSize: () => ({ width }), evaluate: async () => (open() ? [{ at: "div" }] : []) } };
}

test("nothing is replayed while no modal is open in the explorer's tab, and the openers noted meanwhile are dropped", async () => {
  const { calls, page } = tab("https://site.test/lanes", () => false);
  const s = state();
  s.pending.push(opener("Edit lane"));
  await hook.recordModals(page, () => {}, new Set(), s);
  assert.deepStrictEqual([calls, s.pending, s.busy], [[], [], false]);
});

test("each opener is replayed once per page and width, the first replay judges the load, and at most three run", async () => {
  const { calls, page } = tab("https://site.test/lanes?id=4", () => true);
  const s = state(), measure = async (...names) => {
    s.pending.push(...names.map(opener));
    await hook.recordModals(page, () => {}, new Set(), s);
  };
  await measure();                     // open with no opener noted: the page's load
  await measure();                     // the load again: judged
  await measure("Edit lane");          // an opener, and the load counts as judged
  await measure("Edit lane", "Add");   // the first again: judged; the second is the third replay
  await measure("Delete");             // a fourth: over the cap
  assert.deepStrictEqual(calls, [[null, false], ["Edit lane", true], ["Add", true]]);
  // Another width of the same page is its own page.
  const narrow = tab("https://site.test/lanes", () => true, async () => null, 375);
  await hook.recordModals(narrow.page, () => {}, new Set(), s);
  assert.deepStrictEqual(narrow.calls, [[null, false]]);
});

test("one replay runs at a time: a measure meanwhile returns at once, and the opener it noted is replayed after", async () => {
  let release;
  const { calls, page } = tab("https://site.test/lanes", () => true, n => (n === 1 ? new Promise(resolve => { release = resolve; }) : null));
  const s = state();
  s.pending.push(opener("Edit lane"));
  const first = hook.recordModals(page, () => {}, new Set(), s);
  await new Promise(resolve => setImmediate(resolve));
  s.pending.push(opener("Add"));
  await hook.recordModals(page, () => {}, new Set(), s);
  assert.deepStrictEqual(calls, [["Edit lane", false]]);
  release(null);
  await first;
  assert.deepStrictEqual([calls, s.busy], [[["Edit lane", false], ["Add", true]], false]);
});

test("a finding is written once per page, width and dialog, and a replay that throws writes nothing and frees the hook", async () => {
  const found = { at: "div#tour", text: "Welcome tour", detail: "pressing Escape leaves the dialog open (open on load)" };
  const { page } = tab("https://site.test/lanes#top", () => true, n => {
    if (n === 3) throw new Error("Target page, context or browser has been closed");
    return found;
  });
  const written = [], seen = new Set(), s = state();
  s.pending.push(opener("Edit lane"), opener("Add"), opener("Delete"));
  await hook.recordModals(page, line => written.push(line), seen, s);
  assert.deepStrictEqual(written, [{ page: "https://site.test/lanes", width: 1280, rule: "modal-dismiss", ...found }]);
  assert.deepStrictEqual([s.pending, s.busy], [[], false]);
});

// A browser whose windows each show the modals `shows(window)` returns; a window notes the keys pressed, the points
// clicked and whether its opener was pressed. `made` lists each window's context options, routes and whether it closed.
function browser(shows, { findsOpener = true, loads = true } = {}) {
  const made = [], windows = [], gotos = [];
  const newPage = async () => {
    const win = { keys: [], clicks: [], opened: false, index: windows.length };
    windows.push(win);
    return {
      goto: async (url, options) => {
        gotos.push(options.timeout);
        if (!loads) throw new Error("Timeout exceeded");
      },
      waitForLoadState: async () => {},
      waitForTimeout: ms => new Promise(resolve => setTimeout(resolve, ms)),
      evaluate: async () => shows(win),
      evaluateHandle: async () => ({ asElement: () => (findsOpener ? { click: async () => { win.opened = true; } } : null) }),
      keyboard: { press: async key => win.keys.push(key) },
      mouse: { click: async (x, y) => win.clicks.push([x, y]) },
    };
  };
  const newContext = async options => {
    const context = { options, routes: [], sockets: [], closed: false };
    made.push(context);
    return { newPage, route: async (url, handler) => context.routes.push(handler),
      routeWebSocket: async (url, handler) => context.sockets.push(handler), close: async () => { context.closed = true; } };
  };
  return { made, windows, gotos, newContext };
}
const explorer = (b, url = "https://site.test/lanes") => ({ url: () => url, viewportSize: () => ({ width: 1280, height: 800 }),
  context: () => ({ browser: () => b, storageState: async () => (b ? { cookies: [], origins: [] } : assert.fail("its sign-in was read")) }) });
const modal = (extra = {}) => ({ at: "div#tour", name: "Welcome tour", alert: false, edited: false, point: { x: 20, y: 400 }, ...extra });
const replay = (b, pressed = null, loadJudged = false) => realReplay(explorer(b), "https://site.test/lanes", pressed, loadJudged);
const ESCAPE = "pressing Escape leaves the dialog open", CLICK = "a click on the backdrop outside it leaves the dialog open";

test("a replay's windows can only read, each is closed, and every load is bounded by the replay's budget", async () => {
  const b = browser(() => [modal()]);
  assert.deepStrictEqual(await replay(b), { at: "div#tour", text: "Welcome tour",
    detail: `${ESCAPE}, and so does a click on the backdrop outside it (open on load)` });
  assert.deepStrictEqual(b.windows.map(w => [w.keys, w.clicks]), [[["Escape"], []], [[], [[20, 400]]]]);
  for (const context of b.made) {
    assert.deepStrictEqual(context.options, { viewport: { width: 1280, height: 800 }, storageState: { cookies: [], origins: [] }, serviceWorkers: "block" });
    const verdicts = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"].map(method => {
      let verdict;
      context.routes[0]({ request: () => ({ method: () => method }), continue: () => { verdict = "read"; }, abort: () => { verdict = "aborted"; } });
      return verdict;
    });
    assert.deepStrictEqual(verdicts, ["read", "read", "aborted", "aborted", "aborted", "aborted"]);
    // A websocket the page opens is never connected to the server: the handler does nothing with it.
    context.sockets[0](new Proxy({}, { get: (target, key) => assert.fail(`the websocket's ${String(key)} was used`) }));
    assert.ok(context.closed);
  }
  assert.ok(b.gotos.every(timeout => timeout > 0 && timeout <= 20000) && b.gotos[1] < b.gotos[0], String(b.gotos));
  // A load that never finishes ends the replay with nothing, its window closed.
  const stalled = browser(() => [modal()], { loads: false });
  assert.strictEqual(await replay(stalled), null);
  assert.deepStrictEqual(stalled.made.map(c => c.closed), [true]);
});

test("each half is judged only when it can be: an alertdialog, edited input, another modal, no backdrop point", async () => {
  const results = await Promise.all([
    replay(browser(() => [modal({ alert: true })])),                                              // no click half
    replay(browser(() => [modal({ edited: true })])),                                             // not judged
    replay(browser(win => [modal(win.index ? { name: "Tip of the day" } : {})])),                 // the second load shows another
    replay(browser(() => [modal({ point: null })])),                                              // no point to click
    replay(browser(win => (win.keys.length ? [] : [modal()]))),                                   // Escape closes it
    replay(browser(win => (win.keys.length || win.clicks.length ? [] : [modal()]))),              // both close it
  ]);
  assert.deepStrictEqual(results.map(r => r && r.detail), [`${ESCAPE} (open on load)`, null, `${ESCAPE} (open on load)`,
    `${ESCAPE} (open on load)`, `${CLICK} (open on load)`, null]);
});

test("a modal an opener opens is judged after pressing it, never when it cannot be found or the load was judged", async () => {
  const opens = () => browser(win => (win.opened ? [modal({ name: "Edit lane" })] : []));
  const b = opens();
  assert.strictEqual((await replay(b, opener("Edit lane"))).detail, `${ESCAPE}, and so does a click on the backdrop outside it (opened by pressing "Edit lane")`);
  assert.deepStrictEqual(b.windows.map(w => w.opened), [true, true]);
  assert.strictEqual(await replay(browser(win => (win.opened ? [modal()] : []), { findsOpener: false }), opener("Edit lane")), null);
  // A modal open on load is judged as it is, unless the page's load was judged before; then nothing is pressed.
  const onLoad = browser(() => [modal()]);
  assert.strictEqual(await replay(onLoad, opener("Edit lane"), true), null);
  assert.deepStrictEqual(onLoad.windows.map(w => [w.opened, w.keys]), [[false, []]]);
});

test("the page side is self-contained: run as the page script on an empty page, it finds no modal", () => {
  const vm = require("node:vm");
  assert.strictEqual(typeof require("./modal-page.js"), "function");
  assert.ok(dismiss.openScript.includes("function openModals(") && dismiss.watchScript.includes("function watchOpeners("));
  assert.strictEqual(JSON.stringify(vm.runInNewContext(dismiss.openScript, { document: { querySelectorAll: () => [] } })), "[]");
});

test("a page that is not http, https or file, or a browser that gives no second context, is not replayed", async () => {
  const b = browser(() => [modal()]);
  assert.strictEqual(await realReplay(explorer(b, "about:blank"), "about:blank", null, false), null);
  assert.strictEqual(await realReplay(explorer(null), "https://site.test/lanes", null, false), null);
  assert.deepStrictEqual(b.made, []);
});
