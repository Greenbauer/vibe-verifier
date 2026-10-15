// The page record's own tests, with a stand-in for the Playwright page. Run by
// tests/test_qae_console_pages.py, which also drives the real browser where one is installed.
const assert = require("node:assert");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const hook = require("./console-pages.js");

const message = (type, text, url = "https://cdn.example/app.js", lineNumber = 12) =>
  ({ type: () => type, text: () => text, location: () => ({ url, lineNumber }) });
// An uncaught exception or rejection: an Error, or any other value a page threw.
const thrown = value => ({ thrown: value });
const isException = logged => "thrown" in logged;
// A request the page made, with the answer it got (`status`), or none yet.
const request = (method, url, status) => {
  const made = { method: () => method, url: () => url, existingResponse: () => (status ? answer : null) };
  const answer = { status: () => status, request: () => made };
  return made;
};
const digest = line => crypto.createHash("sha256").update(line).digest("hex");

// The hook, run on a tab showing `url` that already logged `earlier` and made the requests `loaded`:
// `log` is the tab logging one more message, `respond` it getting an answer to `request`, `navigate`
// moves it to another page, `record` reads back what the hook wrote.
async function tab(url, earlier = [], loaded = []) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vv-console-pages-"));
  const out = path.join(dir, "not-yet-made");
  const listeners = { console: [], pageerror: [], response: [] };
  const page = {
    url: () => url,
    on: (event, listener) => listeners[event].push(listener),
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
      record: () => fs.existsSync(path.join(out, "console-pages.jsonl"))
        ? fs.readFileSync(path.join(out, "console-pages.jsonl"), "utf8").trim().split("\n").map(line => JSON.parse(line)) : [],
      navigate: next => { url = next; },
    };
  } finally {
    process.argv = argv;
  }
}

test("an error is recorded with the page that logged it and the digest of its console line", async () => {
  const opened = await tab("https://pay.example/checkout?session=secret#top");
  opened.log(message("error", "Failed to load resource: net::ERR_FAILED", "https://api.example/pageviews?id=1", 0));
  assert.deepStrictEqual(opened.record(), [{
    page: "https://pay.example/checkout",
    sha256: digest("Failed to load resource: net::ERR_FAILED @ https://api.example/pageviews?id=1:0"),
  }]);
});

test("only errors are recorded", async () => {
  const opened = await tab("https://pay.example/");
  opened.log(message("warning", "deprecated"));
  opened.log(message("log", "ready"));
  assert.deepStrictEqual(opened.record(), []);
});

test("what the tab logged before the hook ran is recorded too", async () => {
  const opened = await tab("https://pay.example/", [message("error", "early"), message("info", "hello")]);
  assert.deepStrictEqual(opened.record(), [{ page: "https://pay.example/", sha256: digest("early @ https://cdn.example/app.js:12") }]);
});

test("an error is recorded against the page the tab shows when it is logged", async () => {
  const opened = await tab("https://shop.example/");
  opened.navigate("https://pay.example/checkout");
  opened.log(message("error", "boom"));
  assert.deepStrictEqual(opened.record().map(entry => entry.page), ["https://pay.example/checkout"]);
});

test("a message of several lines is recorded by its first, the one the gate reads", async () => {
  const opened = await tab("https://pay.example/");
  opened.log(message("error", "first line\nsecond line"));
  assert.deepStrictEqual(opened.record().map(entry => entry.sha256), [digest("first line")]);
});

test("an uncaught exception is recorded by the first line of its stack, as the console log writes it", async () => {
  const opened = await tab("https://pay.example/checkout?session=secret");
  const error = new Error("boom\nsecond line");
  error.stack = "Error: boom\nsecond line\n    at https://pay.example/app.js:1:49";
  opened.log(thrown(error));
  assert.deepStrictEqual(opened.record(), [{ page: "https://pay.example/checkout", sha256: digest("Error: boom") }]);
});

test("a rejection with a value that is not an Error is recorded by that value", async () => {
  const opened = await tab("https://pay.example/");
  opened.log(thrown("rejected string"));
  const stackless = new Error("no stack");
  stackless.stack = undefined;
  opened.log(thrown(stackless));
  assert.deepStrictEqual(opened.record().map(entry => entry.sha256), [digest("rejected string"), digest("no stack")]);
});

test("an exception the tab threw before the hook ran is recorded too", async () => {
  const opened = await tab("https://pay.example/", [thrown("early throw")]);
  assert.deepStrictEqual(opened.record(), [{ page: "https://pay.example/", sha256: digest("early throw") }]);
});

test("every answer below 400 is recorded by the digest of its method and URL, static or not, and once", async () => {
  // The shape of a consumer QAE run of 2026-10-14: the logo answered on page after page.
  const logo = "https://site.example/_next/image?url=%2Fbranding%2Flogo.svg&w=640&q=75";
  const opened = await tab("https://site.example/");
  opened.respond(request("GET", logo, 200));
  opened.respond(request("GET", logo + "#top", 304));
  opened.respond(request("POST", "https://site.example/developers", 303));
  opened.respond(request("GET", "https://site.example/api/quote", 404));
  opened.respond(request("GET", "https://site.example/api/down", 500));
  const reopened = await tab("https://site.example/developers");
  reopened.respond(request("GET", logo, 200));
  assert.deepStrictEqual(reopened.record(), []);
  assert.deepStrictEqual(opened.record(), [
    { answered: digest("GET " + logo) },
    { answered: digest("POST https://site.example/developers") },
  ]);
  assert.doesNotMatch(JSON.stringify(opened.record()), /site\.example/);
});

test("an answer the tab got before the hook ran is recorded, and a request with none is not", async () => {
  const opened = await tab("https://shop.example/", [], [
    request("GET", "https://shop.example/early.css", 200), request("GET", "https://shop.example/pending.js"),
    request("GET", "https://shop.example/gone.png", 404), request("GET", "data:image/png;base64,AAAA", 200)]);
  assert.deepStrictEqual(opened.record(), [{ answered: digest("GET https://shop.example/early.css") }]);
});

test("the output directory is the one playwright-mcp was given, in either spelling", () => {
  assert.strictEqual(hook.outputDir(["node", "cli.js", "--output-dir", "qae-artifacts", "--save-session"]), "qae-artifacts");
  assert.strictEqual(hook.outputDir(["node", "cli.js", "--output-dir=out/qae"]), "out/qae");
  assert.throws(() => hook.outputDir(["node", "cli.js", "--headless"]), /--output-dir/);
});

// One explorer of several: the hook reads its isolation file and routes every request of the tab.
// `go` hands the tab one request (a stand-in Playwright route) and resolves with what the hook did with it.
// The ids differ from test to test: what the hook learns about records holds for the whole process.
async function guarded(rules) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vv-isolation-"));
  const file = path.join(dir, "isolation.json");
  fs.writeFileSync(file, JSON.stringify(rules));
  const out = path.join(dir, "out");
  let handler;
  const listeners = { console: [], pageerror: [], response: [] };
  const page = {
    url: () => "https://site.example/",
    on: (event, listener) => listeners[event].push(listener),
    route: async (pattern, given) => { assert.strictEqual(pattern, "**/*"); handler = given; },
    consoleMessages: async () => [], pageErrors: async () => [], requests: async () => [],
  };
  const argv = process.argv;
  process.argv = ["node", "cli.js", "--output-dir", out];
  process.env.VV_QAE_ISOLATION = file;
  try {
    await hook.default({ page });
  } finally {
    process.argv = argv;
    delete process.env.VV_QAE_ISOLATION;
  }
  const go = async (method, url, { type = "fetch", accept = "", status = 200, json, text } = {}) => {
    const body = json === undefined ? text || "" : JSON.stringify(json);
    const response = { status: () => status, headers: () => ({ "content-type": json === undefined ? "text/html" : "application/json" }), text: async () => body };
    const did = {};
    await handler({
      request: () => ({ method: () => method, url: () => url, resourceType: () => type, headers: () => ({ accept }) }),
      fallback: async () => { did.sent = true; },
      abort: async reason => { did.aborted = reason; },
      fetch: async () => { did.fetched = true; return response; },
      fulfill: async given => { did.fulfilled = given; },
    });
    return did;
  };
  const created = async (url, answer) => {
    const got = { status: () => 201, url: () => url, headers: () => answer.headers || {}, json: async () => answer.json,
                  request: () => ({ method: () => "POST", url: () => url }) };
    await Promise.all(listeners.response.map(listener => listener(got)));
  };
  const record = () => fs.readFileSync(path.join(out, "console-pages.jsonl"), "utf8").trim().split("\n").map(line => JSON.parse(line));
  return { go, created, record };
}

const OWN = "qae-77.1-e2", OTHER = "qae-77.1-e4";
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const refusal = given => given && given.status === 409 && JSON.parse(given.body).error.startsWith("Vibe Verifier QAE: ");

test("one explorer has no isolation file, and its tab is never routed", async () => {
  const opened = await tab("https://site.example/");
  assert.deepStrictEqual(opened.record(), []);
});

test("a list loses another explorer's records, which are then refused, and keeps every other", async () => {
  const explorer = await guarded({ tag: OWN, protected: [] });
  const list = { surveys: [{ id: id(1), title: `Mine ${OWN}` }, { id: id(2), journeyId: id(5), title: `Theirs ${OTHER}` },
    { id: id(3), title: "Seeded", answers: [{ id: id(6), by: `old ${"qae-76.2-e1"}`, ref: id(4) }] }], count: 3 };
  const did = await explorer.go("GET", "https://api.example/api/surveys?page=1", { json: list });
  assert.deepStrictEqual(did.fulfilled.json, { surveys: [{ id: id(1), title: `Mine ${OWN}` }, { id: id(3), title: "Seeded", answers: [] }], count: 3 });
  for (const method of ["GET", "PUT", "DELETE"]) {
    const refused = await explorer.go(method, `https://api.example/api/surveys/${id(2)}/results`);
    assert.ok(refusal(refused.fulfilled), JSON.stringify(refused));
    assert.ok(!refused.fetched && !refused.sent);
  }
  // The answer's own id was hidden with it (its `ref` was not: only `id` and `_id` name the object itself), and the
  // journey the dropped survey is on stays open.
  assert.ok((await explorer.go("GET", `https://api.example/api/answers/${id(4)}`, { json: {} })).fetched);
  assert.ok((await explorer.go("GET", `https://api.example/api/journeys/${id(5)}/surveys`, { json: [] })).fetched);
  assert.ok(refusal((await explorer.go("GET", `https://api.example/api/answers/${id(6)}`)).fulfilled));
  assert.deepStrictEqual(explorer.record().filter(line => line.refused).map(line => [line.request, line.why, line.location]), [
    ["GET", "PUT", "DELETE"].map(method => `${method} https://api.example/api/surveys/${id(2)}/results`),
    `GET https://api.example/api/answers/${id(6)}`].flat().map(request =>
    [request, "the record is another QAE explorer's", digest(request.split(" ")[1])]));
  assert.ok(explorer.record().filter(line => line.refused).every(line => line.refused === digest(line.request) && line.explorer === OWN));
});

test("a record that is itself another explorer's is refused, and so is every later request on it", async () => {
  const explorer = await guarded({ tag: OWN, protected: [] });
  const url = `https://api.example/api/journeys/${id(11)}/surveys/${id(10)}`;
  const did = await explorer.go("GET", url, { json: { id: id(10), title: `Theirs ${OTHER}` } });
  assert.ok(did.fetched && refusal(did.fulfilled));
  const again = await explorer.go("DELETE", url);
  assert.ok(!again.fetched && refusal(again.fulfilled));
  assert.ok((await explorer.go("GET", `https://api.example/api/journeys/${id(11)}`, { json: {} })).fetched);
});

test("an answer with no other explorer's tag is handed on as the site gave it", async () => {
  const explorer = await guarded({ tag: OWN, protected: [] });
  for (const answer of [{ json: [{ id: id(20), title: `Mine ${OWN}` }] }, { json: { note: "has both", by: OWN, of: OTHER } },
    { text: `<p>${OTHER}</p>` }, { json: [{ title: OTHER }], status: 404 }]) {
    const did = await explorer.go("GET", "https://api.example/api/surveys", answer);
    assert.ok(did.fetched && did.fulfilled.response && !("json" in did.fulfilled), JSON.stringify(answer));
  }
});

test("a fixture a criterion outside the share names may be read and changed, never deleted", async () => {
  const explorer = await guarded({ tag: OWN, protected: [id(30)] });
  const url = `https://api.example/api/journeys/${id(31)}/surveys/${id(30).toUpperCase()}`;
  const deleted = await explorer.go("DELETE", url);
  assert.ok(refusal(deleted.fulfilled) && !deleted.sent);
  assert.match(JSON.parse(deleted.fulfilled.body).error, /fixture a criterion outside this explorer's share uses, so this explorer may not DELETE it/);
  assert.ok((await explorer.go("PUT", url)).sent);
  assert.ok((await explorer.go("GET", url, { json: { id: id(30) } })).fetched);
  // Deleting a record under it, or a record of its own, goes to the site.
  assert.ok((await explorer.go("DELETE", `${url}/answers/${id(32)}`)).sent);
});

test("only a fetch or XHR is read, a stream and a write are sent as they are, and a failed fetch fails", async () => {
  const explorer = await guarded({ tag: OWN, protected: [] });
  assert.deepStrictEqual(await explorer.go("GET", "https://site.example/app.js", { type: "script" }), { sent: true });
  assert.deepStrictEqual(await explorer.go("GET", "https://site.example/events", { accept: "text/event-stream" }), { sent: true });
  assert.deepStrictEqual(await explorer.go("POST", "https://site.example/api/surveys"), { sent: true });
});

test("a 201 names the record the explorer created, and each record a request names is recorded once a method", async () => {
  const explorer = await guarded({ tag: OWN, protected: [] });
  await explorer.created("https://api.example/api/surveys", { json: { id: id(40), title: "x" } });
  await explorer.created("https://api.example/api/surveys", { headers: { location: `/api/surveys/${id(41)}` }, json: { id: "not-an-id" } });
  await explorer.created("https://api.example/api/surveys", { json: { id: "7" } });
  await explorer.go("GET", `https://api.example/api/journeys/${id(42)}/surveys/${id(40)}?x=secret`, { json: {} });
  await explorer.go("GET", `https://api.example/api/journeys/${id(42)}/surveys/${id(40)}`, { json: {} });
  await explorer.go("PUT", `https://api.example/api/journeys/${id(42)}/surveys/${id(40)}`);
  const lines = explorer.record();
  assert.deepStrictEqual(lines[0], { isolation: OWN });
  assert.deepStrictEqual(lines.filter(line => line.created), [{ created: id(40), explorer: OWN }, { created: id(41), explorer: OWN }]);
  const url = `https://api.example/api/journeys/${id(42)}/surveys/${id(40)}`;
  assert.deepStrictEqual(lines.filter(line => line.opened), [
    { opened: id(42), explorer: OWN, request: `GET ${url}` }, { opened: id(40), explorer: OWN, request: `GET ${url}` },
    { opened: id(42), explorer: OWN, request: `PUT ${url}` }, { opened: id(40), explorer: OWN, request: `PUT ${url}` }]);
  assert.doesNotMatch(JSON.stringify(lines), /secret/);
});

test("an isolation file that names no tag stops the browser rather than guarding nothing", async () => {
  await assert.rejects(guarded({ protected: [] }), /names no explorer tag/);
});

test("a record id is a UUID, 24 hex digits or a ULID, as a path segment", () => {
  assert.deepStrictEqual(hook.recordIds(`https://a.example/x/${id(1)}/y/507f1f77bcf86cd799439011/01ARZ3NDEKTSV4RRFFQ69G5FAV/42/abc?q=${id(2)}`),
    [id(1), "507f1f77bcf86cd799439011", "01ARZ3NDEKTSV4RRFFQ69G5FAV"]);
  assert.deepStrictEqual(hook.recordIds("not a url"), []);
});
