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
//
// When the pull request's criteria are split across explorers, which share the site and its data,
// it also keeps this explorer off the others' records. actions/qae-inputs writes the explorer's
// tag and the records it must not delete to qae-inputs/isolation.json in the workspace, the
// directory playwright-mcp runs in (VV_QAE_ISOLATION names another file); one explorer has no file,
// and nothing below runs. Every record an explorer creates carries its tag (the prompt asks it to),
// qae-<run>.<attempt>-e<K>, and for every fetch and XHR the page makes:
//
// - an answer of JSON is handed to the page without any object whose own text carries another
//   explorer's tag (`qae-...-e<N>`, any run): a list loses those items, so the explorer never sees
//   another explorer's survey, quote or order to open. A record that is itself another explorer's
//   is refused, and so is every later request on its id (a record id: a UUID, 24 hex digits or a
//   ULID, as a path segment);
// - a DELETE of a record a criterion outside this explorer's share names (a fixture the pull request
//   declares by its id) is refused;
// - an answer of 201 names a record this explorer created (its `id`, or the Location header's last
//   segment), and every request is recorded once with the record ids its path names:
//
//     {"isolation": "<tag>"}                                     the guard ran, once per tab
//     {"created": "<id>", "explorer": "<tag>"}
//     {"opened": "<id>", "explorer": "<tag>", "request": "<METHOD> <URL, no query>"}
//     {"refused": "<digest of METHOD URL>", "location": "<digest of URL>", "explorer": "<tag>",
//      "request": "<METHOD> <URL, no query>", "why": "..."}
//
// A refused request is answered 409 by the harness, never sent to the site. gates/qae_artifacts.py
// names each refusal, and every record one explorer opened that another created, as a finding: the
// tag is a rule the model follows, and this is where it is checked. Requests a snippet makes through
// Playwright's own request client (page.request) never reach the page, so this sees none of them.
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

// A record id as a path segment names it: a UUID, a MongoDB ObjectId (24 hex digits) or a ULID.
const RECORD_ID = /^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{24}|[0-9A-HJKMNP-TV-Z]{26})$/i;
// An explorer's tag, as bin/vibe-verifier makes it: qae-<run id>.<attempt>-e<K>, qae-<run>-e<K> or qae-e<K>.
const TAG = /\bqae-(?:[0-9]+(?:\.[0-9]+)?-)?e[0-9]+\b/g;

function recordIds(url) {
  let pathname;
  try { pathname = new URL(url).pathname; } catch { return []; }
  return pathname.split("/").map(segment => decodeURIComponent(segment)).filter(segment => RECORD_ID.test(segment));
}

// The isolation file actions/qae-inputs wrote for one of several explorers, or undefined.
function isolation(file = process.env.VV_QAE_ISOLATION || "qae-inputs/isolation.json") {
  if (!fs.existsSync(file)) return undefined;
  const read = JSON.parse(fs.readFileSync(file, "utf8"));
  if (typeof read.tag !== "string" || !read.tag) throw new Error(`${file} names no explorer tag`);
  return { tag: read.tag, protected: new Set(Array.isArray(read.protected) ? read.protected.map(id => String(id).toLowerCase()) : []) };
}

// True when one of the object's own string values carries a tag other than `own`, and none carries `own`.
function foreign(value, own) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const tags = Object.values(value).filter(field => typeof field === "string").flatMap(field => field.match(TAG) || []);
  return tags.length > 0 && !tags.includes(own);
}

// `value` without another explorer's objects, each dropped object's own id (`id` or `_id`) added to `hidden`;
// never the other ids it holds, which name records it belongs to (the journey a survey is on).
// Undefined when `value` is itself another explorer's object.
function strip(value, own, hidden) {
  if (foreign(value, own)) {
    for (const field of [value.id, value._id]) if (typeof field === "string" && RECORD_ID.test(field)) hidden.add(field);
    return undefined;
  }
  if (Array.isArray(value)) return value.map(item => strip(item, own, hidden)).filter(item => item !== undefined);
  if (value && typeof value === "object") {
    const kept = {};
    for (const [key, field] of Object.entries(value)) {
      const stripped = strip(field, own, hidden);
      if (stripped !== undefined) kept[key] = stripped;
    }
    return kept;
  }
  return value;
}

// One process serves every tab: what one tab learns about records holds for the others.
const created = new Set(), hidden = new Set(), opened = new Set();

function guard(rules, write) {
  const note = (request, line) => write({ ...line, explorer: rules.tag, request: `${request.method()} ${request.url().split(/[?#]/)[0]}` });
  const refuse = (route, why) => {
    const request = route.request();
    const sha256 = text => crypto.createHash("sha256").update(text).digest("hex");
    const url = request.url().split("#")[0];
    note(request, { refused: sha256(`${request.method()} ${url}`), location: sha256(url), why });
    return route.fulfill({ status: 409, contentType: "application/json",
      body: JSON.stringify({ error: `Vibe Verifier QAE: ${why}, so this explorer may not ${request.method()} it (harnesses/qae/README.md)` }) });
  };
  return async route => {
    const request = route.request();
    if (!["fetch", "xhr"].includes(request.resourceType())) return route.fallback();
    const method = request.method(), ids = recordIds(request.url());
    if (ids.some(id => hidden.has(id))) return refuse(route, "the record is another QAE explorer's");
    if (method === "DELETE" && rules.protected.has(String(ids[ids.length - 1]).toLowerCase())) {
      return refuse(route, "the record is a fixture a criterion outside this explorer's share uses");
    }
    for (const id of ids.filter(id => !opened.has(`${method} ${id}`))) {
      opened.add(`${method} ${id}`);
      note(request, { opened: id });
    }
    if (method !== "GET" || /event-stream/.test(request.headers().accept || "")) return route.fallback();
    let response;
    try {
      response = await route.fetch();
    } catch {
      return route.abort("failed");
    }
    const text = response.status() < 400 && /json/i.test(response.headers()["content-type"] || "") ? await response.text() : "";
    if (!(text.match(TAG) || []).some(tag => tag !== rules.tag)) return route.fulfill({ response });
    let json;
    try { json = JSON.parse(text); } catch { return route.fulfill({ response }); }
    const kept = strip(json, rules.tag, hidden);
    if (kept === undefined) {
      if (ids.length) hidden.add(ids[ids.length - 1]);
      return refuse(route, "the record is another QAE explorer's");
    }
    return JSON.stringify(kept) === JSON.stringify(json) ? route.fulfill({ response }) : route.fulfill({ response, json: kept });
  };
}

// The record a 201 answer created: the Location header's last segment, or the body's own `id`.
async function createdId(got) {
  const location = recordIds(new URL(got.headers().location || "/", got.url()).href).pop();
  if (location) return location;
  try {
    const body = await got.json();
    return body && typeof body.id === "string" && RECORD_ID.test(body.id) ? body.id : undefined;
  } catch {
    return undefined;
  }
}

exports.default = async ({ page }) => {
  const dir = outputDir(process.argv);
  fs.mkdirSync(dir, { recursive: true });
  const write = line => { if (line) fs.appendFileSync(path.join(dir, "console-pages.jsonl"), JSON.stringify(line) + "\n"); };
  const rules = isolation();
  if (rules) {
    write({ isolation: rules.tag });
    await page.route("**/*", guard(rules, write));
    page.on("response", async got => {
      if (got.status() !== 201 || got.request().method() !== "POST") return;
      const id = await createdId(got);
      if (id && !created.has(id)) {
        created.add(id);
        write({ created: id, explorer: rules.tag });
      }
    });
  }
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
exports.recordIds = recordIds;
exports.strip = strip;
