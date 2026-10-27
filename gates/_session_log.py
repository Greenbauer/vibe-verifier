"""The QAE explorer's session log (session-*/session.md, playwright-mcp's --save-session) as gates/_retry.py reads it:
each tool call's name, the file it saved to, its arguments and its result, in the order called."""
import glob
import json
import os
import re

TOOL_CALL = re.compile(r"^### Tool call: (?P<name>\S+)\s*$", re.MULTILINE)
# What a tool call was told to save its result to, in the call's Args block.
SAVED_TO = re.compile(r'"filename":\s*"(?P<name>[^"\\]+)"')
# Tools that only read the page: a step's action is the other calls it made, in order.
READS = ("browser_snapshot", "browser_console_messages", "browser_network_requests", "browser_take_screenshot",
         "browser_wait_for")
REFS = ("ref", "target", "element")
PAGE_URL = re.compile(r"^- Page URL: (?P<url>\S+)", re.MULTILINE)
ARGS = re.compile(r"```json\n(?P<json>.*?)\n```", re.DOTALL)


def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def tool_calls(root):
    """[(tool name, the file its Args saved to or '', (URL, its other arguments) from inputs, its Result)] per session
    log, in the order called."""
    sessions = []
    for session in sorted(glob.glob(os.path.join(root, "session-*", "session.md"))):
        calls, listed = TOOL_CALL.split(read(session)), []
        for name, body in zip(calls[1::2], calls[2::2]):
            block = ARGS.search(body)  # the Args: the first fenced block (JSON holds no raw newline)
            args, result = (block.group("json"), body[block.end():]) if block else ("", body)
            saved = SAVED_TO.search(args)
            listed.append((name, saved.group("name") if saved else "", inputs(args), result))
        sessions.append(listed)
    return sessions


def unref(value):
    if isinstance(value, dict):
        return {key: unref(field) for key, field in value.items() if key not in REFS}
    return [unref(item) for item in value] if isinstance(value, list) else value


def inputs(args):
    """(URL, every other argument) of a call's Args: where it navigated, and what it typed, filled, selected, pressed or
    ran, all but the element refs and descriptions, which change when the page renders again."""
    try:
        given = json.loads(args)
    except ValueError:
        given = None
    if not isinstance(given, dict):
        return "", ""
    return str(given.get("url") or ""), json.dumps(unref(given), sort_keys=True)


def acts(calls):
    """The calls that changed the page, as (tool, (URL, values)) in order: what a step did, its reads left out."""
    return [(name, given) for name, _, given, _ in calls if name not in READS]


def screenshots(session, item, k, m):
    """(the screenshot before step k's of any step or -1, step k's first, step k's last, step m's) as call indexes, or
    None when the session holds no screenshot of step k before one of step m. A step may be shot more than once."""
    shots = [i for i, call in enumerate(session) if call[0] == "browser_take_screenshot"]
    after = [i for i in shots if session[i][1].endswith("qae/%s-step-%s.png" % (item, m))]
    own = [i for i in shots if after and i < after[0] and session[i][1].endswith("qae/%s-step-%s.png" % (item, k))]
    if not own:
        return None
    return max([i for i in shots if i < own[0]], default=-1), own[0], own[-1], after[0]


def page_urls(result):
    """The URLs a call's Result says the page was at: its `page` field, which playwright-mcp writes, never the result
    a script returned."""
    block = ARGS.search(result)
    try:
        page = json.loads(block.group("json")).get("page")
    except (AttributeError, ValueError):
        return []
    return PAGE_URL.findall(page) if isinstance(page, str) else []
