// A node:test reporter that writes the path of each test file with a failure, once per file, for
// `affected-tests --report`: node's own JUnit reporter does not say which file a test case is in.
//   node --test --test-reporter=spec --test-reporter-destination=stdout \
//     --test-reporter=<this file> --test-reporter-destination=failed-files.txt ...
export default async function* failedFiles(source) {
  const seen = new Set();
  for await (const event of source) {
    if (event.type === "test:fail" && event.data.file && !seen.has(event.data.file)) {
      seen.add(event.data.file);
      yield `${event.data.file}\n`;
    }
  }
}
