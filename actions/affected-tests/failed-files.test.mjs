// The node:test reporter for `affected-tests --report`, fed the events node's runner emits. Run by
// tests/test_affected_tests.py; alone, `node --test actions/affected-tests/failed-files.test.mjs`.
import assert from "node:assert/strict";
import test from "node:test";

import failedFiles from "./failed-files.mjs";

async function* events(list) {
  yield* list;
}

async function written(list) {
  const out = [];
  for await (const chunk of failedFiles(events(list))) out.push(chunk);
  return out.join("");
}

test("writes each file with a failure once, and nothing for passes", async () => {
  const output = await written([
    { type: "test:pass", data: { name: "adds", file: "/repo/a.test.mjs" } },
    { type: "test:fail", data: { name: "rounds", file: "/repo/b.test.mjs" } },
    { type: "test:fail", data: { name: "rounds again", file: "/repo/b.test.mjs" } },
    { type: "test:fail", data: { name: "loads", file: "/repo/c.test.mjs" } },
    { type: "test:diagnostic", data: { message: "tests 4" } },
  ]);
  assert.equal(output, "/repo/b.test.mjs\n/repo/c.test.mjs\n");
});

test("a failure with no file names none", async () => {
  assert.equal(await written([{ type: "test:fail", data: { name: "hook" } }]), "");
});
