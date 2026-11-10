// The width captures' own tests (widths.js), with a stand-in for the Playwright page. Run by tests/test_qae_widths.py;
// tests/test_qae_widths_live.py drives the real browser.
const assert = require("node:assert");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const widths = require("./widths.js");

const temp = () => fs.mkdtempSync(path.join(os.tmpdir(), "vv-widths-"));

// A page at 1280x800 whose accessibility tree reads `trees` in turn and that shows a popup when `popup` is true;
// `take` is its screenshot, which reports the width it was taken at.
function page({ popup = false, trees = ["tree", "tree"] } = {}) {
  let size = { width: 1280, height: 800 };
  const sizes = [];
  return {
    sizes,
    viewportSize: () => size,
    setViewportSize: async given => { size = given; sizes.push(given.width); },
    evaluate: async (fn, arg) => (arg === undefined ? undefined : popup),
    waitForTimeout: async () => {},
    locator: () => ({ ariaSnapshot: async () => trees.shift() }),
    take: async function () { return Buffer.from(`shot at ${size.width}`); },
  };
}

const record = dir => fs.readFileSync(path.join(dir, widths.RECORD), "utf8").trim().split("\n").map(line => JSON.parse(line));
const sha256 = text => crypto.createHash("sha256").update(text).digest("hex");

test("the widths are the file's, once each, whole numbers above zero; none without a file", () => {
  const dir = temp();
  fs.writeFileSync(path.join(dir, "widths"), "1280\n375\n375\n\nmobile\n0\n");
  assert.deepStrictEqual(widths.widths(path.join(dir, "widths")), [1280, 375]);
  assert.deepStrictEqual(widths.widths(path.join(dir, "missing")), []);
});

test("each other width is captured, measured and recorded by the explorer's screenshot, and the window put back", async () => {
  const dir = temp(), tab = page(), measured = [];
  const shot = Buffer.from("the explorer's shot");
  await widths.capture(tab, tab.take, {}, shot, { dir, wanted: [1280, 375, 768], measure: async p => measured.push(p.viewportSize().width) });
  assert.deepStrictEqual(tab.sizes, [375, 768, 1280]);
  assert.deepStrictEqual(measured, [375, 768]);
  const lines = record(dir);
  assert.deepStrictEqual(lines.map(line => [line.shot, line.width, line.disturbed]),
    [[sha256(shot), 375, false], [sha256(shot), 768, false]]);
  assert.strictEqual(fs.readFileSync(path.join(dir, lines[0].file), "utf8"), "shot at 375");
});

test("a page whose tree reads differently after the round trip is recorded as disturbed", async () => {
  const dir = temp(), tab = page({ trees: ["menu open", "menu closed"] });
  await widths.capture(tab, tab.take, {}, Buffer.from("x"), { dir, wanted: [1280, 375] });
  assert.strictEqual(record(dir)[0].disturbed, true);
});

test("an open menu, listbox or tooltip is never resized", async () => {
  const dir = temp(), tab = page({ popup: true });
  await widths.capture(tab, tab.take, {}, Buffer.from("x"), { dir, wanted: [1280, 375] });
  assert.deepStrictEqual(tab.sizes, []);
  assert.deepStrictEqual(record(dir).map(line => line.skipped), ["a menu, listbox or tooltip was open"]);
});

test("a capture that fails puts the window back before the failure reaches the hook", async () => {
  const dir = temp(), tab = page();
  const failing = async () => { throw new Error("tab closed"); };
  await assert.rejects(widths.capture(tab, failing, {}, Buffer.from("x"), { dir, wanted: [1280, 375] }), /tab closed/);
  assert.deepStrictEqual(tab.sizes, [375, 1280]);
});
