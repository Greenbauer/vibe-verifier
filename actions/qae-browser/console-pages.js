// The QAE harness's page record. playwright-mcp runs this for every tab it opens (--init-page).
//
// A line of console-*.log names the script or resource an error is about, never the page that
// logged it: `[ERROR] Failed to load resource: net::ERR_... @ <resource URL>:0` reads the same on
// the site under test and on a third-party page a link opened. So for each console error this
// appends one line to console-pages.jsonl in the run's output directory:
//
//     {"page": "<URL of the page the tab showed, query and fragment dropped>", "sha256": "<digest>"}
//
// The digest is of the error as console-*.log holds it (the first line of `<text> @ <url>:<line>`),
// which is how gates/qae_artifacts.py finds the pages that logged a line. A digest and a URL with
// no query, never the text: this file must not become a second place a credential can land.
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

function entry(pageUrl, message) {
  const at = message.location();
  const line = `${message.text()} @ ${at.url}:${at.lineNumber}`.split("\n")[0];
  return { page: pageUrl.split(/[?#]/)[0], sha256: crypto.createHash("sha256").update(line).digest("hex") };
}

exports.default = async ({ page }) => {
  const dir = outputDir(process.argv);
  fs.mkdirSync(dir, { recursive: true });
  const record = message => {
    if (message.type() === "error") fs.appendFileSync(path.join(dir, "console-pages.jsonl"), JSON.stringify(entry(page.url(), message)) + "\n");
  };
  page.on("console", record);
  // What the tab logged before this ran: a tab a link opened is already loading when it gets here.
  for (const message of await page.consoleMessages()) record(message);
};
exports.outputDir = outputDir;
