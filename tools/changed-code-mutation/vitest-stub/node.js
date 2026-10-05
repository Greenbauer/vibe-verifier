// The Vitest runner plugin imports `vitest/node` from its own install before it loads the project's
// copy, and falls back to this one when the project has none. The gate runs only the project's Vitest.
export function createVitest() {
  throw new Error("vibe-verifier: the project's own vitest could not be loaded from the directory Stryker ran in");
}
