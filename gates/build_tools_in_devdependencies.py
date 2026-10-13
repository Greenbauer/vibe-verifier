#!/usr/bin/env python3
"""Fail when a build, test or type package sits in runtime `dependencies`.

Those packages never run in production. In `dependencies` they are pulled by
every production install, which grows the image and the supply-chain surface,
and they route through the wrong update-review path.

Override, with a reason, in package.json:
    "vibeVerifier": {"allowInDependencies": {"<package>": "<why it must be a runtime dependency>"}}
"""
import json

from _contract import Finding, git, run_gate, tracked_files

GATE = "build-tools-in-devdependencies"

NAMES = {
    "typescript", "ts-node", "tsx", "eslint", "prettier", "vitest", "jest", "mocha", "chai",
    "postcss", "autoprefixer", "tailwindcss", "webpack", "vite", "rollup", "esbuild", "nodemon",
    "husky", "lint-staged", "lefthook", "knip", "jscpd", "dependency-cruiser", "playwright",
    "jsdom", "happy-dom",
}
PREFIXES = (
    "@types/", "eslint-", "@typescript-eslint/", "@eslint/", "prettier-plugin-", "@vitest/",
    "@testing-library/", "@playwright/", "@stryker-mutator/", "@tailwindcss/", "@jest/", "@vitejs/",
)


def is_build_tool(name):
    return name in NAMES or name.startswith(PREFIXES)


def read_manifest(repo, path):
    """The package.json at HEAD as a JSON object, or None when it is not one."""
    try:
        manifest = json.loads(git(repo, "show", "HEAD:" + path))
    except ValueError:
        return None  # no-duplicate-package-json-keys owns malformed JSON
    return manifest if isinstance(manifest, dict) else None


def manifest_findings(path, manifest):
    findings = []
    settings = manifest.get("vibeVerifier")
    allowed = settings.get("allowInDependencies", {}) if isinstance(settings, dict) else {}
    for name in sorted(manifest.get("dependencies") or {}):
        if not is_build_tool(name):
            continue
        if name in allowed:
            if not str(allowed[name]).strip():
                findings.append(Finding('override for "%s" has no reason' % name, path))
            continue
        findings.append(Finding('"%s" is a build, test or type package: move it to devDependencies' % name, path))
    return findings


def check(args):
    findings = []
    for path in tracked_files(args.repo):
        if path.rsplit("/", 1)[-1] != "package.json" or "node_modules/" in path:
            continue
        manifest = read_manifest(args.repo, path)
        if manifest is not None:
            findings += manifest_findings(path, manifest)
    return findings


if __name__ == "__main__":
    run_gate(GATE, __doc__, check)
