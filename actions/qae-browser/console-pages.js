// The QAE harness's page record. playwright-mcp runs this for every tab it opens (--init-page).
//
// A line of console-*.log names the script or resource an error is about, never the page that
// logged it: `[ERROR] Failed to load resource: net::ERR_... @ <resource URL>:0` reads the same on
// the site under test and on a third-party page a link opened, and an uncaught exception's stack
// reads the same too. So for each console error and uncaught exception this appends one line to
// console-pages.jsonl in the run's output directory:
//
//     {"page": "<URL of the page the tab showed, query and fragment dropped>", "sha256": "<digest>"}
//
// The digest is of the error as console-*.log holds it, which is how gates/qae_artifacts.py finds
// the pages that logged a line: the first line of `<text> @ <url>:<line>` for a console error, and
// for an uncaught exception or rejection (the page's `pageerror`, which the log writes with no level
// tag) the first line of the error's stack, or of the value thrown when it is not an Error.
//
// It also appends one line per request a page got an answer below 400 for, once per request:
//
//     {"answered": "<digest of `<METHOD> <URL, fragment dropped>`>"}
//
// The explorer's own network record (browser_network_requests) leaves out every successful image,
// font, script and stylesheet unless it passes `static: true`, and lists one the page cancelled, so
// the gate could never find the answer that excuses a cancelled static resource (a consumer QAE run
// of 2026-10-14: a navbar logo answered on every earlier page, cancelled on the last one). The
// gate reads these lines as answers: a request is named as the network record names it, method and
// URL, which is how gates/qae_artifacts.py matches a cancelled line to its answer.
//
// Digests and a URL with no query, never the text: this file must not become a second place a
// credential can land.
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

// The directory playwright-mcp was told to write to: `--output-dir DIR` or `--output-dir=DIR`.
function outputDir(argv) {
  const at = argv.findIndex(arg => arg === "--output-dir" || arg.startsWith("--output-dir="));
  const dir = at < 0 ? undefined : argv[at].includes("=") ? argv[at].slice(argv[at].indexOf("=") + 1) : argv[at + 1];
  if (!dir) throw new Error("console-pages.js needs playwright-mcp's --output-dir: the page record is written there");
  return dir;
}

// What console-*.log holds for each, as playwright-mcp renders it (messageToConsoleMessage and
// pageErrorToConsoleMessage in its bundle, @playwright/mcp 0.0.81).
function consoleLine(message) {
  const at = message.location();
  return `${message.text()} @ ${at.url}:${at.lineNumber}`;
}

function exceptionLine(error) {
  return error instanceof Error ? error.stack || error.message : String(error);
}

function entry(pageUrl, logged) {
  const line = logged.split("\n")[0];
  return { page: pageUrl.split(/[?#]/)[0], sha256: crypto.createHash("sha256").update(line).digest("hex") };
}

// One process serves every tab, so a request answered on one page is written once for the run.
const answered = new Set();

function answer(request) {
  const named = `${request.method()} ${request.url().split("#")[0]}`;
  if (!/^https?:/.test(request.url()) || answered.has(named)) return undefined;
  answered.add(named);
  return { answered: crypto.createHash("sha256").update(named).digest("hex") };
}

exports.default = async ({ page }) => {
  const dir = outputDir(process.argv);
  fs.mkdirSync(dir, { recursive: true });
  const write = line => { if (line) fs.appendFileSync(path.join(dir, "console-pages.jsonl"), JSON.stringify(line) + "\n"); };
  const record = logged => write(entry(page.url(), logged));
  const message = logged => { if (logged.type() === "error") record(consoleLine(logged)); };
  const exception = error => record(exceptionLine(error));
  const response = got => { if (got.status() < 400) write(answer(got.request())); };
  page.on("console", message);
  page.on("pageerror", exception);
  page.on("response", response);
  // What the tab logged and loaded before this ran: a tab a link opened is already loading when it gets here.
  for (const logged of await page.consoleMessages()) message(logged);
  for (const error of await page.pageErrors()) exception(error);
  for (const request of await page.requests()) if (request.existingResponse()) response(request.existingResponse());
};
exports.outputDir = outputDir;
