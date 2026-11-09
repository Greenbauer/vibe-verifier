// A stand-in for the Playwright page the page record hook (console-pages.js) is handed, shared by its node tests
// (console-pages.test.js, network-record.test.js).
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const hook = require("./console-pages.js");

const message = (type, text, url = "https://cdn.example/app.js", lineNumber = 12) =>
  ({ type: () => type, text: () => text, location: () => ({ url, lineNumber }) });
// An uncaught exception or rejection: an Error, or any other value a page threw.
const thrown = value => ({ thrown: value });
const isException = logged => "thrown" in logged;
// A request the page made, with the answer it got (`status`), or none yet.
// `failed` is the error text of a request that got no answer.
const request = (method, url, status, failed) => {
  const made = { method: () => method, url: () => url, existingResponse: () => (status ? answer : null),
                 failure: () => (failed ? { errorText: failed } : null) };
  const answer = { status: () => status, statusText: () => (status === 200 ? "OK" : status === 500 ? "Internal Server Error" : ""),
                   request: () => made };
  return made;
};
const digest = line => crypto.createHash("sha256").update(line).digest("hex");

// The hook, run on a tab showing `url` that already logged `earlier` and made the requests `loaded`:
// `log` is the tab logging one more message, `respond` it getting an answer to `request`, `navigate`
// moves it to another page, `record` reads back what the hook wrote.
async function tab(url, earlier = [], loaded = []) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vv-console-pages-"));
  const out = path.join(dir, "not-yet-made");
  const listeners = { console: [], pageerror: [], response: [], requestfailed: [], request: [] };
  const mainFrame = {};
  const page = {
    url: () => url,
    on: (event, listener) => listeners[event].push(listener),
    mainFrame: () => mainFrame,
    consoleMessages: async () => earlier.filter(logged => !isException(logged)),
    pageErrors: async () => earlier.filter(isException).map(logged => logged.thrown),
    requests: async () => loaded,
  };
  const argv = process.argv;
  process.argv = ["node", "cli.js", "--output-dir", out];
  try {
    await hook.default({ page });
    return {
      log: logged => listeners[isException(logged) ? "pageerror" : "console"].forEach(listener => listener(isException(logged) ? logged.thrown : logged)),
      respond: made => listeners.response.forEach(listener => listener(made.existingResponse())),
      fail: made => listeners.requestfailed.forEach(listener => listener(made)),
      // The tab making `made`, a request that is not a navigation, or loading the next document.
      make: made => listeners.request.forEach(listener => listener(Object.assign(made, { isNavigationRequest: () => false, frame: () => mainFrame }))),
      load: () => listeners.request.forEach(listener => listener({ isNavigationRequest: () => true, frame: () => mainFrame })),
      record: () => fs.existsSync(path.join(out, "console-pages.jsonl"))
        ? fs.readFileSync(path.join(out, "console-pages.jsonl"), "utf8").trim().split("\n").map(line => JSON.parse(line)) : [],
      network: () => fs.readFileSync(path.join(out, "network-pages.log"), "utf8"),
      navigate: next => { url = next; },
    };
  } finally {
    process.argv = argv;
  }
}

module.exports = { digest, isException, message, request, tab, thrown };
