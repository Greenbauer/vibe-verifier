"""A 4xx the site answered to a request the QAE explorer's own script sent, as the qae-artifacts gate reads it.

The explorer may run a script in the page (browser_evaluate, browser_run_code, browser_run_code_unsafe), and a script
can `import()` or `fetch()` a path the explorer made up: on Greenbauer/zack.land PR 197 one imported
`/_next/static/chunks/components/Background/skyState.js`, a module path no page requests, and the dev server's 404
failed the run. The page never asked for it, so it is not the site's failure. A 4xx answer is the explorer's own when:

- Chromium's console line for it (`Failed to load resource: the server responded with a status of 404`, at its URL)
  is one of the console entries playwright-mcp says a script call logged (the call's `events`, `New console entries:
  <file>#La-Lb`);
- that call's script names the request's URL as written: the whole URL, or its path (with its query, if it has one)
  standing alone, not part of a longer path;
- every such console line of that status and URL in the run is one a naming script call logged, and the run's network
  records list that answer under no more request numbers than there are of those lines (the harness's network-*.log
  alone when there is one, each file numbering its own requests), so a page that sent the same request too is still
  judged.

The excuse is by status and URL, as a declared refusal's is, and reaches both the request and its console line. A 5xx
(the server failing whoever asked), a request that failed outright, an uncaught exception and any other console error
are never excused: a script call's console entries are what appeared while it ran, which may be the page's own. What
the gate cannot read: whether a page that sent the very same request while the script ran was the one answered.
"""
import glob
import json
import os
import re
import urllib.parse

from _probe import SCRIPTS, Console, requests
from _retry import exact, shown
from _session_log import console_lines, network_texts, read, session_calls, tool_calls

ENTRY = re.compile(r"^\[ERROR\] Failed to load resource: the server responded with a status of (?P<status>4[0-9]{2})\b"
                   r".* @ (?P<url>\S+):[0-9]+$")
# A character that would continue a URL's path or query: a name the script holds must not be part of a longer one.
URL_CHAR = r"\w\-.~/%?=&#+"


def script_text(args):
    """Every string a script call's Args hold (its function or code), joined: what the explorer wrote."""
    try:
        given = json.loads(args)
    except ValueError:
        return args
    found, stack = [], [given]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return "\n".join(found)


def names(text, url):
    """True when the script `text` names `url` as written: the whole URL, or its path and query on their own."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path + ("?" + parts.query if parts.query else "")
    origin = "%s://%s" % (parts.scheme, parts.netloc)
    if not path.strip("/"):
        return False
    pattern = r"(?<![%s])(?:%s)?%s(?![%s])" % (URL_CHAR, re.escape(origin), re.escape(path), URL_CHAR)
    return re.search(pattern, text) is not None


def scripted(root, console):
    """{(status, URL): {(log, line)}} of the 4xx console lines script calls logged whose script names the URL."""
    found = {}
    for session in session_calls(root):
        for name, args, result in session:
            if name not in SCRIPTS:
                continue
            text = script_text(args)
            for place in console_lines(root, result):
                refused = ENTRY.match(console.entry(place) or "")
                if refused and names(text, exact(refused.group("url"))):
                    found.setdefault((refused.group("status"), exact(refused.group("url"))), set()).add(place)
    return found


def everywhere(console):
    """{(status, URL): {(log, line)}} of every 4xx Chromium console line in the run."""
    found = {}
    for path, lines in console.lines.items():
        for number in range(1, len(lines) + 1):
            refused = ENTRY.match(console.entry((path, number)) or "")
            if refused:
                found.setdefault((refused.group("status"), exact(refused.group("url"))), set()).add((path, number))
    return found


def scripted_excuses(root):
    """The (status, URL pattern) pairs the explorer's own scripts excuse, as the gate's expected refusals are written.
    Each one is printed, so the log says what was excused and why, or why not."""
    console = Console(root)
    found = scripted(root, console)
    if not found:
        return []
    every = everywhere(console)
    harness = [read(path) for path in sorted(glob.glob(os.path.join(root, "network-*.log")))]
    texts = harness or network_texts(root, tool_calls(root))
    excused = []
    for (status, url), lines in sorted(found.items()):
        listed = requests(texts, status, url, per_file=bool(harness))
        if every.get((status, url), set()) - lines or len(listed) > len(lines):
            print("qae-artifacts: %s %s, which the explorer's own script requested, was requested by the page too: "
                  "not excused" % (status, shown(url)))
            continue
        print("qae-artifacts: %s %s was answered to a request the explorer's own script sent (it names that URL), "
              "not one the page sent: excused" % (status, shown(url)))
        excused.append((status, re.compile(re.escape(url))))
    return excused
