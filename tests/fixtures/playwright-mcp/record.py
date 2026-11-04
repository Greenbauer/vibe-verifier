#!/usr/bin/env python3
"""Record what the pinned playwright-mcp writes, once, into the fixtures beside this file.

The qae-artifacts gate and its parsers read files playwright-mcp writes, and a format they assumed has more than once
been a guess a real run proved wrong. So their tests read real output: this script serves the site site.json describes,
drives the pinned playwright-mcp with each QAE lane's own arguments (read from harnesses/qae/explore.yml and
actions/qae-codex/action.yml) the way an explorer does, and keeps what it wrote under <scenario>/ beside this file.
manifest.json says what it was recorded with and what each page did. tests/test_qae_recorded.py fails when
tools/qae-browser/package-lock.json or a lane's arguments change and the fixtures were not recorded again.

Run it after either changes, from the repository root, with node on PATH:

    python3 tests/fixtures/playwright-mcp/record.py

It installs the toolchain from the lockfile (bin/vibe-verifier tool qae-browser) and that build's chromium. The site
listens on fixed local ports, so every URL, and the page record's digests of them, read the same on every recording.
What still varies is replaced with fixed text, in file names and wherever a file names them: a session folder's epoch
milliseconds (session-946684800001) and the timestamps in console-*.log and page-*.yml names (2000-01-01T00-00-00-001Z,
...), each in the order recorded. Console offsets are kept as recorded. Screenshots and the layout and copy records are
not kept: no parser under test reads them, and the tests write a placeholder where a step names a screenshot.
"""
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ORIGINS = {"{SITE}": "http://127.0.0.1:47811", "{OTHER}": "http://127.0.0.1:47812", "{CLOSED}": "http://127.0.0.1:47813",
           "{SOCKET}": "ws://127.0.0.1:47813"}
SITE, OTHER, CLOSED = ORIGINS["{SITE}"], ORIGINS["{OTHER}"], ORIGINS["{CLOSED}"]
NAME, VALUE = "QAE_PASSWORD", "fixture'tick`quote\"slash\\-0123456789"
COOKIE = "fixture-cookie-0123456789"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z")
EPOCH = re.compile(r"session-([0-9]{13})")
DROPPED = (".png", "ui-audit.jsonl", "copy.jsonl")
# The reason phrase each status is sent with, which the network record and Chromium's console line repeat: Python's own
# changed for 422 in 3.13, and a recording must not depend on the Python that made it.
REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 422: "Unprocessable Content",
           500: "Internal Server Error"}
LOGO = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'


def placed(value):
    """`value` (text, or lists and dicts of it) with the origins in place of {SITE}, {OTHER}, {CLOSED}, {SOCKET}."""
    if isinstance(value, str):
        for token, origin in ORIGINS.items():
            value = value.replace(token, origin)
        return value
    if isinstance(value, dict):
        return {key: placed(item) for key, item in value.items()}
    return [placed(item) for item in value]


SITE_JSON = placed(json.loads((HERE / "site.json").read_text()))


class Origin:
    """One origin on its fixed port. `routes` maps (METHOD, path) to (status, content type, body, seconds to wait), or to
    a function of the request's body that returns one; `log` keeps (METHOD, path, status, Cookie header) per request."""

    def __init__(self, url, routes):
        log = self.log = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self):
                sent = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                route = routes.get((self.command, self.path.split("?")[0]), (404, "text/plain", "", 0))
                status, kind, body, wait = route(sent) if callable(route) else route
                log.append((self.command, self.path.split("?")[0], status, self.headers.get("Cookie", "")))
                time.sleep(wait)
                try:
                    self.send_response(status, REASONS[status])
                    for header, value in (("Content-Type", kind), ("Content-Length", str(len(body.encode()))),
                                          ("Cache-Control", "no-store"), ("Access-Control-Allow-Origin", "*")):
                        self.send_header(header, value)
                    self.end_headers()
                    self.wfile.write(body.encode())
                except OSError:
                    pass  # the page cancelled it

            do_GET = do_POST = answer

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", int(url.rsplit(":", 1)[1])), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def json_answer(status, wait=0):
    return (status, "application/json", "{}", wait)


def routes(pages, more):
    found = {("GET", path): (200, "text/html", text, 0) for path, text in pages.items()}
    found[("GET", "/favicon.ico")] = (200, "image/x-icon", "", 0)
    found.update(more)
    return found


def site_routes():
    orders = []

    def quantity(sent):
        value = str(json.loads(sent).get("quantity", ""))
        return json_answer(200 if value.isdigit() and int(value) > 0 else 422)

    def order_list(sent):
        orders.append(sent)
        return json_answer(500 if len(orders) == 1 else 200)

    return routes(SITE_JSON["pages"], {
        ("GET", "/api/missing"): json_answer(404), ("GET", "/api/private"): json_answer(401),
        ("GET", "/api/boom"): json_answer(500), ("GET", "/api/items"): json_answer(200, 0.5),
        ("GET", "/api/slow"): json_answer(200, 3), ("GET", "/api/orders"): order_list,
        ("GET", "/img/logo.svg"): (200, "image/svg+xml", LOGO, 1), ("GET", "/img/never.svg"): (200, "image/svg+xml", LOGO, 3),
        ("POST", "/api/quantity"): quantity,
        ("POST", "/auth/token"): lambda sent: json_answer(200 if json.loads(sent).get("password") == VALUE else 400)})


def lane_arguments():
    """{lane: playwright-mcp's arguments after its entry point}, as each lane writes them: the Claude template's
    --mcp-config and the Codex action's `args`. A file is named by its file name, and a value the run fills in by
    <storage-state> or <config>."""
    config = re.search(r"--mcp-config '(.+)'", (ROOT / "harnesses" / "qae" / "explore.yml").read_text()).group(1)
    claude = json.loads(config)["mcpServers"]["playwright"]["args"][1:]
    action = (ROOT / "actions" / "qae-codex" / "action.yml").read_text()
    codex = json.loads("[%s]" % "".join(re.findall(r'^ *args\+?="(.*)"$', action, re.MULTILINE)).replace('\\"', '"'))[1:]
    named = {"$ARTIFACTS": "qae-artifacts", "$state": "<storage-state>", "$config": "<config>"}
    return {"claude": [re.sub(r"^\$\{\{ steps\.browser\.outputs\.([a-z-]+) \}\}$", r"\1.js", arg) for arg in claude],
            "codex": [named.get(arg, os.path.basename(arg) if "/" in arg else arg) for arg in codex]}


def lockfile():
    """({package: version} of every package the toolchain's lockfile pins, the lockfile's sha256)."""
    data = (ROOT / "tools" / "qae-browser" / "package-lock.json").read_bytes()
    packages = json.loads(data)["packages"]
    return ({path.split("node_modules/")[-1]: entry["version"] for path, entry in packages.items() if path},
            hashlib.sha256(data).hexdigest())


class Cleanups:
    """What Explorer expects of a test: somewhere to register its cleanups."""

    def addCleanup(self, *args):
        pass


def ref(answer, role, name):
    return re.search(r'- %s "%s" \[ref=([^\]]+)\]' % (role, name), answer["result"]["content"][0]["text"]).group(1)


def shot(explore, k):
    explore("browser_take_screenshot", filename="qae-artifacts/qae/AC1-step-%d.png" % k)


def errors(explore):
    explore("browser_navigate", url=SITE + "/errors")
    explore("browser_wait_for", time=2)
    shot(explore, 1)
    explore("browser_network_requests")
    explore("browser_network_requests", static=True)
    explore("browser_network_requests", filename="qae-artifacts/qae/AC1-network.txt")
    explore("browser_console_messages")
    explore("browser_console_messages", filename="qae-artifacts/qae/AC1-step-1-console.txt")
    return "- step 1: opened /errors -> saw the heading\n"


def cancel(explore):
    for k, path in enumerate(("/dashboard", "/developers"), 1):
        explore("browser_navigate", url=SITE + path)
        explore("browser_wait_for", time=2)
        shot(explore, k)
        explore("browser_network_requests", filename="qae-artifacts/qae/AC1-network%s.txt" % path.replace("/", "-"))
    return "- step 1: opened /dashboard -> saw the logo\n- step 2: opened /developers -> saw the heading\n"


def tab(explore):
    explore("browser_navigate", url=SITE + "/shop")
    explore("browser_click", element="Pay", target="#pay")
    explore("browser_wait_for", time=2)
    shot(explore, 1)
    explore("browser_network_requests")
    explore("browser_console_messages")
    return "- step 1: opened /shop and followed Pay -> saw the hosted checkout\n"


def probe(explore):
    explore("browser_navigate", url=SITE + "/form")
    shot(explore, 1)
    form = explore("browser_snapshot")
    explore("browser_type", element="Quantity", target=ref(form, "textbox", "Quantity"), text="-1")
    explore("browser_snapshot")
    explore("browser_click", element="Save", target=ref(form, "button", "Save"))
    explore("browser_wait_for", text="positive number")
    explore("browser_snapshot")
    shot(explore, 2)
    explore("browser_network_requests")
    explore("browser_console_messages")
    return ("- step 1: opened /form -> saw the order form\n- step 2: invalid input: typed -1 in Quantity and saved -> "
            "the form said the quantity must be a positive number\n")


def retry(explore):
    explore("browser_navigate", url=SITE + "/orders")
    explore("browser_wait_for", text="Something went wrong")
    shot(explore, 1)
    explore("browser_network_requests", filename="qae-artifacts/qae/AC1-step-1-network.txt")
    explore("browser_console_messages", filename="qae-artifacts/qae/AC1-step-1-console.txt")
    explore("browser_navigate", url=SITE + "/orders")
    explore("browser_wait_for", text="2 orders")
    shot(explore, 2)
    explore("browser_network_requests")
    return "- step 1: opened /orders -> saw Something went wrong\n- step 2: retry of step 1: opened /orders -> saw 2 orders\n"


def secrets(explore):
    field = "input[type=password]"
    explore("browser_navigate", url=SITE + "/login")
    explore("browser_type", element="Email address", target="input[type=email]", text="qae@example.test")
    explore("browser_type", element="Password", target=field, text=NAME)
    explore("browser_click", element="Sign in", target="button")
    explore("browser_wait_for", text="Signed in (1)")
    explore("browser_fill_form", fields=[{"name": "Password", "type": "textbox", "target": field, "value": NAME}])
    explore("browser_click", element="Sign in", target="button")
    explore("browser_wait_for", text="Signed in (2)")
    explore("browser_run_code_unsafe", code="async (page) => { await page.fill('%s', '%s'); await page.click('button'); "
            "await page.getByText('Signed in (3)').waitFor(); return page.url(); }" % (field, NAME))
    explore("browser_run_code_unsafe", code="async (page) => await page.inputValue('%s')" % field)
    explore("browser_evaluate", function="() => document.querySelector('%s').value" % field)
    shot(explore, 1)
    explore("browser_network_requests")
    explore("browser_console_messages")
    return "- step 1: signed in with the password typed, filled and entered by a snippet -> saw Signed in (3)\n"


SCENARIOS = {"errors": ("claude", errors), "cancel": ("claude", cancel), "tab": ("claude", tab),
             "probe": ("claude", probe), "retry": ("claude", retry), "secrets": ("codex", secrets)}


def lane_extra(lane, work):
    """What the lane passes after the page record's --init-page, which Explorer passes itself."""
    hook = str(ROOT / "actions" / "qae-browser") + "/%s"
    if lane == "claude":
        return ("--init-page", hook % "ui-audit.js")
    state, config = os.path.join(work, "state.json"), os.path.join(work, "mcp.json")
    Path(state).write_text(json.dumps({"origins": [], "cookies": [
        {"name": "session", "value": COOKIE, "domain": "127.0.0.1", "path": "/", "expires": -1, "httpOnly": True,
         "secure": False, "sameSite": "Lax"}]}))
    Path(config).write_text(json.dumps({"secrets": {"VV_COOKIE_1": COOKIE, NAME: VALUE}}))
    return ("--init-page", hook % "ui-audit.js", "--storage-state", state, "--config", config,
            "--init-page", hook % "secret-names.js")


def given(argv, work):
    """playwright-mcp's arguments after its entry point, named as lane_arguments() names them."""
    named = {os.path.join(work, "state.json"): "<storage-state>", os.path.join(work, "mcp.json"): "<config>"}
    return [named.get(arg, os.path.basename(arg) if "/" in arg else arg) for arg in argv[2:]]


def record(name, lane, walk, origins):
    # The client the browser tests drive playwright-mcp with. Imported here, so tests/test_qae_recorded.py can read
    # this file's lane_arguments() and lockfile() on any commit.
    sys.path.insert(0, str(ROOT / "tests"))
    from test_qae_console_pages import Explorer
    work = tempfile.mkdtemp(prefix="vv-record-")
    try:
        root = os.path.join(work, "qae-artifacts")
        os.makedirs(os.path.join(root, "qae"))
        calls, explorer = [], Explorer(Cleanups(), root, lane_extra(lane, work))
        if given(explorer.process.args, work) != lane_arguments()[lane]:
            raise SystemExit("the explorer's arguments are not the %s lane's: %s" % (lane, explorer.process.args))

        def explore(tool, **arguments):
            calls.append([tool, arguments.get("filename", "")])
            return explorer.call(tool, **arguments)
        for origin in origins:
            origin.log.clear()
        Path(root, "qae", "AC1.md").write_text(walk(explore))
        explore("browser_close")
        explorer.close()
        target = HERE / name
        shutil.rmtree(target, True)
        shutil.copytree(root, target, ignore=lambda _, names: [n for n in names if n.endswith(DROPPED)])
        normalize(target, work)
        return dict(SITE_JSON["truth"][name], lane=lane, calls=calls,
                    screenshots=[file.split("/", 1)[1] for tool, file in calls if tool == "browser_take_screenshot"],
                    served=sorted("%s %s%s %d" % (method, url, path, status) for url, origin in zip((SITE, OTHER), origins)
                                  for method, path, status, _ in origin.log),
                    cookie=any(COOKIE in cookie for origin in origins for *_, cookie in origin.log))
    finally:
        shutil.rmtree(work, True)


def normalize(target, work):
    """Replace the epoch milliseconds and timestamps that name files, in the names and in every file, and the
    recording's own directory, with fixed text in the order recorded."""
    files = sorted(path for path in target.rglob("*") if path.is_file())
    names = [str(path.relative_to(target)) for path in files]
    found = sorted({stamp for name in names for stamp in STAMP.findall(name)})
    epochs = sorted({epoch for name in names for epoch in EPOCH.findall(name)})
    texts = {**{stamp: "2000-01-01T00-00-00-%03dZ" % n for n, stamp in enumerate(found, 1)},
             **{"session-" + epoch: "session-%d" % (946684800000 + n) for n, epoch in enumerate(epochs, 1)},
             work: "/tmp/vv-record"}
    for path in files:
        text = path.read_text()
        for old, new in texts.items():
            text = text.replace(old, new)
        path.write_text(text)
    for path in sorted(target.rglob("*"), key=lambda path: -len(path.parts)):
        name = path.name
        for old, new in texts.items():
            name = name.replace(old, new)
        if name != path.name:
            path.rename(path.with_name(name))


def main():
    with socket.socket() as closed:
        if closed.connect_ex(("127.0.0.1", int(CLOSED.rsplit(":", 1)[1]))) == 0:
            raise SystemExit("something listens on %s, which must refuse connections" % CLOSED)
    tool = subprocess.run([sys.executable, str(ROOT / "bin" / "vibe-verifier"), "tool", "qae-browser"],
                          check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run([os.path.join(tool, "node_modules", ".bin", "playwright"), "install", "chromium"], check=True)
    os.environ["QAE_MCP"] = os.path.join(tool, "node_modules", "@playwright", "mcp", "cli.js")
    os.environ.pop("QAE_CONSOLE_PAGES", None)
    origins = (Origin(SITE, site_routes()), Origin(OTHER, routes(SITE_JSON["other_pages"], {
        ("GET", "/api/data"): json_answer(200), ("GET", "/api/boom"): json_answer(500)})))
    versions, digest = lockfile()
    manifest = {"record": "python3 tests/fixtures/playwright-mcp/record.py", "versions": versions,
                "lockfile_sha256": digest, "lanes": lane_arguments(),
                "origins": {token.strip("{}").lower(): url for token, url in ORIGINS.items()},
                "secret": {"name": NAME, "value": VALUE, "cookie": COOKIE},
                "scenarios": {name: record(name, lane, walk, origins) for name, (lane, walk) in SCENARIOS.items()}}
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print("recorded %s with @playwright/mcp %s" % (", ".join(SCENARIOS), versions["@playwright/mcp"]))


if __name__ == "__main__":
    main()
