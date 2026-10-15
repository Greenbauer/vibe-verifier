"""The static import graph that affected-test selection walks (gates/_affected_tests.py).

Every JS, TS and Python file the head tracks is read once for what it imports, and each import is
resolved to the repository files the project's own tooling could load for it:

- JS and TS: a relative specifier (`./x`, `../x.js`), trying the extensions and `index` files Node,
  TypeScript and Vite try, a `.js` specifier also finding its `.ts` source; a `paths` alias or the
  `baseUrl` of the nearest tsconfig.json (or jsconfig.json) above the importing file, following its
  relative `extends`; and a workspace package's name, to its `exports`, `main`, `module` or `types`
  entry, its `src/index` or `index`, and its subpaths. `vi.mock`, `jest.mock`, `importActual` and
  `require` of a literal path count as imports.
- Python: `import a.b` and `from a.b import c`, absolute or relative, resolved by dotted-path suffix,
  so `pkg.mod` finds `src/pkg/mod.py` whatever the import root is, and the packages it sits in.

A specifier that could name more than one file is an edge to each: an extra edge selects an extra
test, a missing one misses one. Paths the pull request deleted still resolve, so a test that still
imports a removed file is reached by the removal. Imports are read by pattern, not parsed: one inside
a comment or a string counts too.
"""
import json
import os
import posixpath
import re

JS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_EXTENSION_TRIES = ("",) + JS_EXTENSIONS
_INDEX_TRIES = tuple("/index" + ext for ext in JS_EXTENSIONS)
_TS_FOR_JS = (".ts", ".tsx", ".mts", ".cts")
# `import x from "a"`, `export * from "a"`, `import "a"`, and a call naming a literal path: `import("a")`,
# `require("a")`, `vi.mock("a")`, `jest.requireActual("a")`, `vi.importActual("a")`.
JS_IMPORT = re.compile(r"""\b(?:import|export)\s[^'"`;]*?\bfrom\s*['"]([^'"]+)['"]"""
                       r"""|\bimport\s*['"]([^'"]+)['"]"""
                       r"""|\b(?:require|import|mock|doMock|requireActual|importActual|requireMock|importMock)"""
                       r"""\s*\(\s*['"]([^'"]+)['"]""")
PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*[\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n;]*)", re.M)
PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]+as[ \t]+\w+)?(?:[ \t]*,[ \t]*[\w.]+(?:[ \t]+as[ \t]+\w+)?)*)", re.M)
# A quoted relative path, or a quoted path with a directory and an extension: what a test runner's
# config names its setup files by (`setupFiles: ["./vitest.setup.ts"]`).
NAMED_PATH = re.compile(r"""['"](\.{1,2}/[^'"\s]+|[\w@.-]+(?:/[\w@.-]+)+\.\w+)['"]""")
# JSON with comments, as tsconfig files are written: a string is kept, a comment is dropped.
_JSONC = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.S)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def read_jsonc(text):
    """The value of a JSON file that may hold comments and trailing commas, or None when it is not JSON."""
    try:
        return json.loads(_TRAILING_COMMA.sub(r"\1", _JSONC.sub(lambda m: m.group(1) or "", text)))
    except ValueError:
        return None


class Graph:
    """Who imports whom among the files of one working tree."""

    def __init__(self, root, files, deleted=()):
        self.root = root
        self.files = sorted(files)
        self.known = set(files) | set(deleted)  # what an import may resolve to
        self._texts = {}
        self._scopes = self._tsconfig_scopes()
        self._packages = self._workspace_packages()
        self._modules = {}
        for path in sorted(self.known):
            if path.endswith(".py"):
                self._index_module(path)

    def text(self, path):
        """The file's text at the head, '' when it is gone or unreadable."""
        if path not in self._texts:
            try:
                with open(os.path.join(self.root, path), encoding="utf-8", errors="replace") as handle:
                    self._texts[path] = handle.read()
            except OSError:
                self._texts[path] = ""
        return self._texts[path]

    def imports(self, path):
        """The repository files `path` imports."""
        if path.endswith(JS_EXTENSIONS):
            found = set()
            for match in JS_IMPORT.finditer(self.text(path)):
                found |= self.resolve_js(path, next(group for group in match.groups() if group))
            return found
        if path.endswith(".py"):
            return self._python_imports(path)
        return set()

    def named_paths(self, path):
        """The repository files `path` names in a quoted path, relative to its own directory (a directory's
        index file is not one: a config naming `./src` as an alias root does not load it)."""
        here = posixpath.dirname(path)
        return set().union(*(self._try(posixpath.join(here, name), index=False) for name in NAMED_PATH.findall(self.text(path))))

    def importers(self, extra=None):
        """{file: the files that import it}; `extra` maps a file to more files it reads."""
        reverse = {}
        for path in self.files:
            for target in self.imports(path) | (extra or {}).get(path, set()):
                reverse.setdefault(target, set()).add(path)
        return reverse

    # JS and TS

    def resolve_js(self, source, spec):
        if spec.startswith("."):
            return self._try(posixpath.join(posixpath.dirname(source), spec))
        found = set()
        aliases, base_url = self._scope(source)
        for pattern, targets in aliases:
            for target in _alias_targets(spec, pattern, targets):
                found |= self._try(target)
        if base_url is not None:
            found |= self._try(posixpath.join(base_url, spec))
        return found | self._package_files(spec)

    def _try(self, base, index=True):
        base = posixpath.normpath(base)
        tries = [base + ending for ending in _EXTENSION_TRIES + (_INDEX_TRIES if index else ())]
        stem, ext = posixpath.splitext(base)
        if ext in (".js", ".jsx", ".mjs", ".cjs"):
            tries += [stem + ending for ending in _TS_FOR_JS]
        return {path for path in tries if path in self.known}

    def _scope(self, source):
        """The aliases and baseUrl of the nearest directory above `source` whose tsconfig defines any."""
        directory = posixpath.dirname(source)
        while True:
            if directory in self._scopes:
                return self._scopes[directory]
            if not directory:
                return [], None
            directory = posixpath.dirname(directory)

    def _tsconfig_scopes(self):
        scopes = {}
        for path in self.files:
            if re.match(r"(?:ts|js)config[^/]*\.json$", posixpath.basename(path)):
                aliases, base_url = self._compiler_paths(path, set())
                if aliases or base_url is not None:
                    have, known_base = scopes.get(posixpath.dirname(path), ([], None))
                    scopes[posixpath.dirname(path)] = (have + aliases, known_base if known_base is not None else base_url)
        return scopes

    def _compiler_paths(self, path, seen):
        """([(pattern, [target])], baseUrl) for one tsconfig, its `paths` taken from it or what it extends."""
        seen.add(path)
        config = read_jsonc(self.text(path))
        options = _compiler_options(config)
        here = posixpath.dirname(path)
        base_url = _base_url(options, here)
        if isinstance(options.get("paths"), dict):
            return _aliases(options["paths"], here if base_url is None else base_url), base_url
        for parent in _extends(config, here):
            if parent in self.known and parent not in seen:
                aliases, parent_base = self._compiler_paths(parent, seen)
                if aliases or parent_base is not None:
                    return aliases, base_url if base_url is not None else parent_base
        return [], base_url

    def _workspace_packages(self):
        """{package name: (its directory, its manifest)} for every tracked package.json with a name."""
        packages = {}
        for path in self.files:
            if posixpath.basename(path) == "package.json":
                manifest = read_jsonc(self.text(path))
                if isinstance(manifest, dict) and isinstance(manifest.get("name"), str):
                    packages[manifest["name"]] = (posixpath.dirname(path), manifest)
        return packages

    def _package_files(self, spec):
        found = set()
        for name, (directory, manifest) in self._packages.items():
            if spec == name:
                entries = _strings(_export(manifest.get("exports"), ".")) + [
                    manifest.get(key) for key in ("main", "module", "types", "typings") if isinstance(manifest.get(key), str)]
                entries += ["src/index", "index"]
            elif spec.startswith(name + "/"):
                rest = spec[len(name) + 1:]
                entries = _strings(_export(manifest.get("exports"), "./" + rest)) + [rest, "src/" + rest]
            else:
                continue
            for entry in entries:
                found |= self._try(posixpath.join(directory, entry))
        return found

    # Python

    def _index_module(self, path):
        dotted = path[:-3].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[:-len(".__init__")]
        parts = dotted.split(".")
        for start in range(len(parts)):
            self._modules.setdefault(".".join(parts[start:]), set()).add(path)

    def _python_imports(self, path):
        text = self.text(path)
        found = set()
        for match in PY_FROM.finditer(text):
            names = [part.split(" as ")[0].strip() for part in re.split(r"[,\n]", re.sub(r"#[^\n]*", "", match.group(2)).strip("()"))]
            found |= self._resolve_python(path, match.group(1), [name for name in names if name and name != "*"])
        for match in PY_IMPORT.finditer(text):
            for part in match.group(1).split(","):
                found |= self._resolve_python(path, part.split(" as ")[0].strip(), [])
        return found

    def _resolve_python(self, source, module, names):
        if module.startswith("."):
            package = posixpath.dirname(source)
            for _ in range(len(module) - len(module.lstrip(".")) - 1):
                package = posixpath.dirname(package)
            rest = module.lstrip(".")
            base = posixpath.join(package, *rest.split(".")) if rest else package
            candidates = [base] + [posixpath.join(base, name) for name in names]
            return {path for candidate in candidates for path in (candidate + ".py", candidate + "/__init__.py")
                    if path in self.known}
        found = set()
        for dotted in [module] + [module + "." + name for name in names]:
            parts = dotted.split(".")
            for end in range(1, len(parts) + 1):  # importing a.b.c runs a and a.b too
                found |= self._modules.get(".".join(parts[:end]), set())
        return found


def _alias_targets(spec, pattern, targets):
    """What a tsconfig `paths` pattern maps `spec` to: `@/*` with `src/*` maps `@/lib/x` to `src/lib/x`."""
    if "*" not in pattern:
        return list(targets) if spec == pattern else []
    prefix, _, suffix = pattern.partition("*")
    if len(spec) < len(prefix) + len(suffix) or not (spec.startswith(prefix) and spec.endswith(suffix)):
        return []
    star = spec[len(prefix):len(spec) - len(suffix)]
    return [target.replace("*", star) for target in targets]


def _compiler_options(config):
    options = config.get("compilerOptions") if isinstance(config, dict) else None
    return options if isinstance(options, dict) else {}


def _base_url(options, here):
    value = options.get("baseUrl")
    return posixpath.normpath(posixpath.join(here, value)) if isinstance(value, str) else None


def _aliases(paths, root):
    """[(pattern, [target])] from a tsconfig `paths`, each target relative to `root` (its baseUrl or directory)."""
    return [(pattern, [posixpath.join(root, target) for target in targets if isinstance(target, str)])
            for pattern, targets in paths.items() if isinstance(targets, list)]


def _extends(config, here):
    """The tsconfig files a config extends by relative path (a package's shared config is not read)."""
    value = config.get("extends") if isinstance(config, dict) else None
    names = value if isinstance(value, list) else [value]
    paths = []
    for name in names:
        if isinstance(name, str) and name.startswith("."):
            path = posixpath.normpath(posixpath.join(here, name))
            paths.append(path if path.endswith(".json") else path + ".json")
    return paths


def _export(exports, key):
    """A package.json `exports` entry for one subpath (`.` or `./x`); the whole value is `.` when it maps no subpaths."""
    if not isinstance(exports, dict) or not any(name.startswith(".") for name in exports):
        return exports if key == "." else None
    for pattern, value in exports.items():
        if pattern == key:
            return value
        star = _alias_targets(key, pattern, ["*"]) if "*" in pattern else []
        if star:
            return _substitute(value, star[0])
    return None


def _substitute(value, star):
    if isinstance(value, str):
        return value.replace("*", star)
    if isinstance(value, dict):
        return {name: _substitute(item, star) for name, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, star) for item in value]
    return value


def _strings(value):
    """Every string in an `exports` value, conditions included: each is a file the package may load."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def reach(importers, starts):
    """Every file that reaches one of `starts` through imports, `starts` included."""
    seen, stack = set(starts), list(starts)
    while stack:
        for path in importers.get(stack.pop(), ()):
            if path not in seen:
                seen.add(path)
                stack.append(path)
    return seen
