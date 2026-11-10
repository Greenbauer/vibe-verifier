// The network record the page hook writes (console-pages.js, network-pages.log), with a stand-in for the Playwright
// page. Run by tests/test_qae_console_pages.py; tests/test_qae_network_record.py drives the real browser.
const assert = require("node:assert");
const { test } = require("node:test");

const { request, tab } = require("./console-pages.stub.js");

test("every http(s) request answered or failed is a network record line, numbered for the run, query kept, once", async () => {
  const opened = await tab("https://site.example/", [], [request("GET", "https://site.example/early.css", 200),
                                                         request("GET", "https://site.example/pending.js")]);
  const down = request("GET", "https://site.example/api/items?page=2", 500);
  opened.respond(down);
  opened.respond(down);
  opened.fail(request("GET", "https://site.example/logo.svg", undefined, "net::ERR_ABORTED"));
  opened.respond(request("GET", "data:image/png;base64,AAAA", 200));
  const lines = opened.network().trim().split("\n");
  assert.deepStrictEqual(lines.map(line => line.replace(/^[0-9]+\. /, "")), [
    "[GET] https://site.example/early.css => [200] OK",
    "[GET] https://site.example/api/items?page=2 => [500] Internal Server Error",
    "[GET] https://site.example/logo.svg => [FAILED] net::ERR_ABORTED",
  ]);
  const numbers = lines.map(line => Number(line.split(".")[0]));
  assert.deepStrictEqual(numbers, [...numbers].sort((a, b) => a - b));
  assert.strictEqual(new Set(numbers).size, numbers.length);
});

test("a request cancelled because the tab loaded another document is left out, one cancelled on its own page is not", async () => {
  // A framework's prefetch, cancelled when the explorer clicks through to the next page: the explorer's
  // record starts over with the new document and never listed it, so the gate must not see it either.
  const opened = await tab("https://site.example/");
  opened.load();
  const prefetch = request("GET", "https://site.example/next?_rsc=1", undefined, "net::ERR_ABORTED");
  const dropped = request("GET", "https://site.example/logo.svg", undefined, "net::ERR_ABORTED");
  const refused = request("GET", "https://site.example/api/slow", undefined, "net::ERR_CONNECTION_RESET");
  for (const made of [prefetch, dropped, refused]) opened.make(made);
  opened.fail(dropped);
  opened.load();
  opened.fail(prefetch);
  opened.fail(refused);
  assert.deepStrictEqual(opened.network().trim().split("\n").map(line => line.replace(/^[0-9]+\. /, "")), [
    "[GET] https://site.example/logo.svg => [FAILED] net::ERR_ABORTED",
    "[GET] https://site.example/api/slow => [FAILED] net::ERR_CONNECTION_RESET",
  ]);
});

test("a tab that loaded nothing still leaves an empty network record", async () => {
  const opened = await tab("https://site.example/");
  assert.strictEqual(opened.network(), "");
});
