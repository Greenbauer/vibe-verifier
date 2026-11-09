// The QAE harness's width captures. ui-audit.js installs this on every tab playwright-mcp opens (--init-page).
//
// A repository may ask for each criterion to be seen at more than one viewport width (the qae-artifacts gate's
// --widths, which actions/qae-inputs writes to qae-inputs/widths, one per line). The explorer used to do that
// itself: resize, reach the state again, screenshot, resize back. In 660 walked runs of the subscribed
// repositories (2026-11-09) that was 27% of its tool calls, and of 185 distinct FAIL verdicts 2 were defects only
// a width showed, both a page that scrolls sideways, which the layout audit measures without a model.
//
// So the harness does it. Every time the explorer saves a screenshot of the viewport (browser_take_screenshot,
// which calls Page.screenshot with no clip), and before that call returns, the same page is resized in place to
// each other width the file lists, given two frames and a moment to lay out, screenshotted, measured by the layout
// audit at that width, and put back. Each capture goes to widths/<digest>-<width>.png in the run's output
// directory, and one line to widths.jsonl:
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
// while a transient popup is open (a menu, listbox or tooltip the next action may need), and `disturbed` says when
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
// A popup the explorer's next action may be about to use, which a resize could close.
const TRANSIENT = "[role=menu], [role=listbox], [role=tooltip], [role=combobox][aria-expanded=true]";

// The widths qae-inputs/widths lists (VV_QAE_WIDTHS names another file); [] without one.
function widths(file = process.env.VV_QAE_WIDTHS || "qae-inputs/widths") {
  if (!fs.existsSync(file)) return [];
  return [...new Set(fs.readFileSync(file, "utf8").split(/\s+/).filter(Boolean).map(Number).filter(n => Number.isInteger(n) && n > 0))];
}

const sha256 = data => crypto.createHash("sha256").update(data).digest("hex");

async function settle(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.waitForTimeout(SETTLE);
}

async function popupOpen(page) {
  return page.evaluate(selector => [...document.querySelectorAll(selector)].some(el => {
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && getComputedStyle(el).visibility !== "hidden";
  }), TRANSIENT);
}

const tree = page => page.locator("body").ariaSnapshot({ timeout: 2000 }).catch(() => "");

// Captures `page` at each of `wanted` other than its own width, with Playwright's screenshot `take` and the
// explorer's `options`, after the explorer's screenshot `shot`; `measure` is the layout audit for the page.
async function capture(page, take, options, shot, { dir, wanted, measure }) {
  const size = page.viewportSize();
  const others = size ? wanted.filter(width => width !== size.width) : [];
  if (!others.length) return;
  const write = line => fs.appendFileSync(path.join(dir, RECORD), JSON.stringify(line) + "\n");
  const digest = sha256(shot);
  if (await popupOpen(page)) {
    write({ shot: digest, skipped: "a menu, listbox or tooltip was open" });
    return;
  }
  const before = await tree(page);
  const taken = [];
  try {
    for (const width of others) {
      await page.setViewportSize({ width, height: size.height });
      await settle(page);
      const file = `widths/${digest.slice(0, 16)}-${width}.png`;
      fs.mkdirSync(path.join(dir, "widths"), { recursive: true });
      fs.writeFileSync(path.join(dir, file), await take.call(page, { ...options, path: undefined }));
      if (measure) await measure(page);
      taken.push({ width, file });
    }
  } finally {
    await page.setViewportSize(size);
    await settle(page).catch(() => {});
  }
  const disturbed = (await tree(page)) !== before;
  for (const one of taken) write({ shot: digest, ...one, disturbed });
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
  fs.appendFileSync(path.join(dir, RECORD), "");
}

exports.install = install;
exports.widths = widths;
exports.capture = capture;
exports.RECORD = RECORD;
