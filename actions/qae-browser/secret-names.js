// The QAE harness's secret names. playwright-mcp runs this for every tab it opens (--init-page).
//
// A site step hands the explorer a NAME for each credential (QAE_PASSWORD), never the value, and
// playwright-mcp types the value when the explorer enters the name. It does that in two tools only,
// browser_fill_form and browser_type. Every other way to enter text sends the name itself, and the
// one a model reaches for is browser_run_code_unsafe, which hands its code the page:
//
//     await page.locator('input[type=password]').fill('QAE_PASSWORD')
//
// That signed in with the literal name, the site answered 400, and the qae-artifacts gate failed a
// run whose criteria all passed, on a sign-in the harness itself got wrong (2026-10-08). So the name
// is resolved one layer down, in the Playwright client every tool and every run-code snippet goes
// through: text that is exactly a secret's name becomes its value in
//
//     Frame.fill / Frame.type          (page.fill, page.type, and a locator's fill, type and
//                                       pressSequentially all end here)
//     ElementHandle.fill / .type
//     Keyboard.type / .insertText
//
// The names and values are the `secrets` of the JSON file playwright-mcp was given as --config, the
// same map its own two tools read, so a name means one thing however it is entered. Without that
// file there is nothing to resolve and nothing is wrapped. A method this build no longer has stops
// the tab from opening: a silent miss would bring the failed sign-in back.
//
// Not covered, because neither goes through a text-entry call: a field a script sets inside the
// page (browser_evaluate), and a name pressed one key at a time (browser_press_key).
const fs = require("fs");

const WRAPPED = Symbol.for("vibe-verifier.secret-names");

// The file playwright-mcp was given as `--config FILE` or `--config=FILE`; undefined when none.
function configFile(argv) {
  const at = argv.findIndex(arg => arg === "--config" || arg.startsWith("--config="));
  return at < 0 ? undefined : argv[at].includes("=") ? argv[at].slice(argv[at].indexOf("=") + 1) : argv[at + 1];
}

// {NAME: value} for every secret of that file with a value, as playwright-mcp's own lookup reads it.
function secrets(argv) {
  const file = configFile(argv);
  const named = file ? JSON.parse(fs.readFileSync(file, "utf8")).secrets || {} : {};
  return new Map(Object.entries(named).filter(([, value]) => typeof value === "string" && value));
}

// Wrap `owner[method]` so that its argument number `at`, when it is exactly a secret's name, is the
// secret's value.
function wrap(owner, what, method, at, named) {
  const original = owner[method];
  if (typeof original !== "function") throw new Error(`secret-names.js: this Playwright build has no ${what}.${method} to resolve secret names in`);
  owner[method] = function (...args) {
    if (named.has(args[at])) args[at] = named.get(args[at]);
    return original.apply(this, args);
  };
}

exports.default = async ({ page }) => {
  // The classes are the process's, so the first tab wraps them for every later one.
  const frame = Object.getPrototypeOf(page.mainFrame());
  if (frame[WRAPPED]) return;
  const named = secrets(process.argv);
  if (!named.size) return;
  // An element handle's class is reachable only through a handle. The first tab is still blank when
  // this runs, so its document is there to ask.
  const handle = await page.mainFrame().evaluateHandle("document");
  const element = Object.getPrototypeOf(handle);
  const keyboard = Object.getPrototypeOf(page.keyboard);
  for (const [owner, what, method, at] of [
    [frame, "Frame", "fill", 1], [frame, "Frame", "type", 1],
    [element, "ElementHandle", "fill", 0], [element, "ElementHandle", "type", 0],
    [keyboard, "Keyboard", "type", 0], [keyboard, "Keyboard", "insertText", 0],
  ]) wrap(owner, what, method, at, named);
  await handle.dispose();
  frame[WRAPPED] = true;
};
exports.configFile = configFile;
exports.secrets = secrets;
