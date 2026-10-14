// The Vitest runner plugin imports `vitest/node` from its own install, then loads the project's copy
// and falls back to this one when that load throws, discarding the error. The gate runs only the
// project's Vitest, so this repeats the plugin's load to say why it failed.
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

export async function createVitest() {
  let reason = "it loaded on a second attempt";
  try {
    const require = createRequire(path.join(process.cwd(), "package.json"));
    await import(pathToFileURL(require.resolve("vitest/node")).href);
    require(require.resolve("vitest/package.json"));
  } catch (error) {
    reason = String(error?.message || error);
  }
  throw new Error("vibe-verifier: the project's own vitest could not be loaded from the directory Stryker ran in: " + reason);
}
