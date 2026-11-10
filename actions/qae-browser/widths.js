// The QAE harness's width captures. ui-audit.js installs this on every tab playwright-mcp opens (--init-page).
//
// A repository may ask for each criterion to be seen at more than one viewport width (the qae-artifacts gate's
// --widths, which actions/qae-inputs writes to qae-inputs/widths, one per line). The explorer used to do that
// itself: resize, reach the state again, screenshot, resize back. In 660 walked runs of the subscribed
// repositories (2026-11-09) that was 27% of its tool calls, and none of 185 distinct FAIL verdicts came from it: the two
// about a width were at a width the criterion itself named, which the explorer still walks.
//
// So the harness does it. Every time the explorer saves a screenshot of the viewport (browser_take_screenshot,
// which calls Page.screenshot with no clip), and before that call returns, the same page is resized in place to
// each other width the file lists, given two frames (at most a second) and a moment to lay out, screenshotted,
// measured by the layout audit at that width, and put back. Each capture goes to widths/<digest>-<width>.png in
// the run's output directory, and one line to widths.jsonl:
//
//     {"shot": "<sha256 of the explorer's screenshot>", "width": 375, "file": "widths/<...>-375.png",
//      "disturbed": false}
//
// The qae-artifacts gate finds a step's captures by its screenshot's digest, so neither the hook nor the gate
// needs the file name the explorer chose, which playwright-mcp only picks after the screenshot is taken.
// Every step is captured, not only a criterion's end state, which no hook can tell apart from the steps before it.
//
// In place, because the state a step shows (a dialog, a filled form, a selected row) is often not at any address
// a second window could load. The explorer's own width checks resized in place too, in 72% of the 3,665 sampled.
// A resize can change a page's state (a menu that closes, a component a breakpoint swaps), so a capture is skipped
// while a popup floats over the page (a menu, listbox or tooltip the next action may need), and `disturbed` says when
// the page's accessibility tree read differently after the round trip than before it. The capture runs inside
// the explorer's own tool call, so it never races the explorer's next action.
//
// Never the explorer's problem: anything that fails here leaves the explorer's screenshot as it was taken.
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const WRAPPED = Symbol.for("vibe-verifier.widths");
const RECORD = "widths.jsonl";
const SETTLE = 250;   // ms after two frames: a layout that animates into place (a drawer, a reflowing grid)
// The longest the two frames are waited for. A page that stubs requestAnimationFrame (an explorer freezing an intro
// animation to time it, as both of a consumer's did on 2026-11-10) never draws another frame, and an unbounded wait
// held the explorer's screenshot call until its job timed out, 30 minutes later, with nothing walked.
const FRAMES_WAIT = 1000;
// The longest one width's capture may take (resize, frames, screenshot, layout audit) before the whole capture is
// given up: the page can stall the hook in ways no single wait above anticipates, and the hook must never hold the
// explorer. A width took about 1.5 s in the live tests. Given up, the window is put back and the step is recorded
// as skipped, as a step a popup covered is.
const CAPTURE_WAIT = 8000;
const TIMED_OUT = Symbol("timed out");
// A popup the explorer's next action may be about to use, which a resize could close: a menu, listbox or tooltip that
// shows and floats over the page (it, or a wrapper up to FLOAT_DEPTH levels above, is positioned absolute or fixed, as
// a dropdown's or tooltip's popper is). A menu in the page's flow (navigation marked role=menu) is not one, and neither
// is a trigger's aria-expanded: a subscribed app's sidebar toggle says expanded for as long as the sidebar shows, and
// read as an open popup it stopped every capture on every page (2026-11-09). Neither is a menu in a header or
// navigation landmark (a fixed header's role=menu navigation floats as a dropdown does, and is always there), nor one
// smaller than MIN_POPUP pixels a side (a tooltip kept for screen readers): either would stop every capture too. A
// dropdown a header opens still counts when its library portals it out of the header (Radix, MUI); one rendered in
// place there (Headless UI without an anchor) is resized like the rest of the page, and `disturbed` records it.
const POPUP = "[role=menu], [role=listbox], [role=tooltip]";
const STANDING = "header, nav, [role=banner], [role=navigation], [role=menubar]";
const FLOAT_DEPTH = 4;
const MIN_POPUP = 8;

// The widths qae-inputs/widths lists (VV_QAE_WIDTHS names another file); [] without one.
function widths(file = process.env.VV_QAE_WIDTHS || "qae-inputs/widths") {
  if (!fs.existsSync(file)) return [];
  return [...new Set(fs.readFileSync(file, "utf8").split(/\s+/).filter(Boolean).map(Number).filter(n => Number.isInteger(n) && n > 0))];
}

const sha256 = data => crypto.createHash("sha256").update(data).digest("hex");

// `promise`'s value, or `late` when `ms` pass first. Timed here, in the hook, not in the page: a page that stubs
// requestAnimationFrame may stub its timers too.
async function within(promise, ms, late) {
  let timer;
  try {
    return await Promise.race([promise, new Promise(resolve => { timer = setTimeout(() => resolve(late), ms); })]);
  } finally {
    clearTimeout(timer);
  }
}

async function settle(page) {
  const frames = page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  frames.catch(() => {});   // abandoned when the wait runs out; the tab may close under it later
  await within(frames, FRAMES_WAIT);
  await page.waitForTimeout(SETTLE);
}

async function popupOpen(page) {
  return page.evaluate(([selector, standing, depth, least]) => [...document.querySelectorAll(selector)].some(el => {
    const box = el.getBoundingClientRect(), style = getComputedStyle(el);
    if (box.width < least || box.height < least || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    if (el.parentElement && el.parentElement.closest(standing)) return false;
    for (let node = el, level = 0; node && node !== document.body && level <= depth; node = node.parentElement, level++) {
      if (["absolute", "fixed"].includes(getComputedStyle(node).position)) return true;
    }
    return false;
  }), [POPUP, STANDING, FLOAT_DEPTH, MIN_POPUP]);
}

// A text field holding what the explorer typed and has not sent. A page that swaps layouts at a breakpoint by
// rendering its route again (a split screen that becomes the phone page below 768px) drops that draft on the resize,
// and the explorer's next action then lands on an empty form it never left (a consumer's survey builder, 2026-11-10).
// The draft is the step's state, so it is never resized. "Typed" is an `input` event the page received: a field a
// framework keeps in sync (React copies each value it renders into defaultValue, so comparing the two never shows
// typing) and a contenteditable editor are seen, and a value a script set is not. A field counts while it is still on
// the page, enabled and not empty; a form's submit or reset ends its fields' drafts. Choices (a box, a slider, a file)
// are not drafts.
const TRACK_TYPING = () => {
  if (window.__vibeVerifierTyped) return;
  const typed = (window.__vibeVerifierTyped = new Set());
  addEventListener("input", event => {
    const field = event.composedPath ? event.composedPath()[0] : event.target;
    if (field && field.nodeType === 1) typed.add(field);
  }, true);
  const sent = event => typed.forEach(field => event.target.contains(field) && typed.delete(field));
  addEventListener("submit", sent, true);
  addEventListener("reset", sent, true);
};
const CHOICES = ["checkbox", "radio", "range", "color", "file", "hidden", "submit", "button", "image", "reset"];
async function draftTyped(page) {
  return page.evaluate(choices => {
    const typed = window.__vibeVerifierTyped;
    if (!typed) return false;
    for (const field of [...typed]) {
      if (!field.isConnected || field.disabled) {
        typed.delete(field);
        continue;
      }
      if (field.tagName === "INPUT" && choices.includes(field.type)) continue;
      const text = field.isContentEditable ? field.textContent : "value" in field ? field.value : "";
      if (String(text || "").trim()) return true;
    }
    return false;
  }, CHOICES).catch(() => false);
}

const tree = page => page.locator("body").ariaSnapshot({ timeout: 2000 }).catch(() => "");

// Captures `page` at each of `wanted` other than its own width, with Playwright's screenshot `take` and the
// explorer's `options`, after the explorer's screenshot `shot`; `measure` is the layout audit for the page. All of it
// within `budget` ms a width, or none of it is recorded and the window is put back.
async function capture(page, take, options, shot, { dir, wanted, measure, budget = CAPTURE_WAIT }) {
  const size = page.viewportSize();
  const others = size ? wanted.filter(width => width !== size.width) : [];
  if (!others.length) return;
  const write = line => fs.appendFileSync(path.join(dir, RECORD), JSON.stringify(line) + "\n");
  const digest = sha256(shot);
  if (await popupOpen(page)) {
    write({ shot: digest, skipped: "a menu, listbox or tooltip was open" });
    return;
  }
  if (await draftTyped(page)) {
    write({ shot: digest, skipped: "a text field held unsent input" });
    return;
  }
  const run = { over: false };
  const work = captures(page, take, options, { dir, digest, others, size, measure, run });
  work.catch(() => {});   // a failure after the budget ran out has no one left to tell
  const done = await within(work, budget * others.length, TIMED_OUT);
  if (done === TIMED_OUT) {
    run.over = true;   // the abandoned capture resizes nothing more: the explorer has the page back
    await page.setViewportSize(size).catch(() => {});
    await settle(page).catch(() => {});   // laid out at its own width again before the explorer's next action
    write({ shot: digest, skipped: "the capture took longer than %d ms a width".replace("%d", budget) });
    return;
  }
  for (const one of done.taken) write({ shot: digest, ...one, disturbed: done.disturbed });
}

// The captures themselves, stopped between steps once `run.over` says the budget ran out.
async function captures(page, take, options, { dir, digest, others, size, measure, run }) {
  const before = await tree(page);
  const taken = [];
  try {
    for (const width of others) {
      if (run.over) break;
      await page.setViewportSize({ width, height: size.height });
      await settle(page);
      if (run.over) break;
      const image = await take.call(page, { ...options, path: undefined });
      if (run.over) break;
      const file = `widths/${digest.slice(0, 16)}-${width}.png`;
      fs.mkdirSync(path.join(dir, "widths"), { recursive: true });
      fs.writeFileSync(path.join(dir, file), image);
      if (measure) await measure(page);
      taken.push({ width, file });
    }
  } finally {
    if (!run.over) {
      await page.setViewportSize(size);
      await settle(page).catch(() => {});
    }
  }
  return { taken, disturbed: (await tree(page)) !== before };
}

// Wraps Page.screenshot, once for the process: the class is shared by every tab. `pages` maps a tab to what its
// capture needs, set by install.
const pages = new WeakMap();

function wrap(page) {
  const owner = Object.getPrototypeOf(page);
  if (owner[WRAPPED]) return;
  const original = owner.screenshot;
  if (typeof original !== "function") throw new Error("widths.js: this Playwright build has no Page.screenshot to capture widths after");
  let capturing = false;
  owner.screenshot = async function (options = {}) {
    const shot = await original.call(this, options);
    const setup = pages.get(this);
    if (!setup || capturing || options.clip || options.path) return shot;   // a hook's own sample, or a script's file
    capturing = true;
    try {
      await capture(this, original, options, shot, setup);
    } catch {
      // the tab navigated or closed mid-capture: the explorer's screenshot stands
    } finally {
      capturing = false;
    }
    return shot;
  };
  owner[WRAPPED] = true;
}

// Captures every viewport screenshot of `page` at the other widths qae-inputs/widths lists, into `dir`, measured
// by `measure`. Nothing without the file, or with one width only.
function install(page, dir, measure) {
  const wanted = widths();
  if (wanted.length < 2) return;
  wrap(page);
  pages.set(page, { dir, wanted, measure });
  // Typing is tracked from the page's first document on, and on the one already open.
  page.addInitScript(TRACK_TYPING).catch(() => {});
  page.evaluate(TRACK_TYPING).catch(() => {});
  fs.appendFileSync(path.join(dir, RECORD), "");
}

exports.install = install;
exports.widths = widths;
exports.capture = capture;
exports.RECORD = RECORD;
exports.FRAMES_WAIT = FRAMES_WAIT;
exports.CAPTURE_WAIT = CAPTURE_WAIT;
