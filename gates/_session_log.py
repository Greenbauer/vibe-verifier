"""The QAE explorer's session log (session-*/session.md, playwright-mcp's --save-session) as gates/_retry.py reads it:
each tool call's name, the file it saved to, its arguments and its result, in the order called."""
import glob
import json
import os
import re

TOOL_CALL = re.compile(r"^### Tool call: (?P<name>\S+)\s*$", re.MULTILINE)
# What a tool call was told to save its result to, in the call's Args block.
SAVED_TO = re.compile(r'"filename":\s*"(?P<name>[^"\\]+)"')
# The tools the pinned playwright-mcp (0.0.81) types readOnly or assertion: a step's action is the other calls it
# made, in order. A tool not listed here counts as changing the page, which only ever takes a retry away.
READS = frozenset("""annotate console_clear console_messages cookie_get cookie_list find generate_locator get_config
    hide_highlight highlight localstorage_get localstorage_list network_clear network_request network_requests pdf_save
    route_list sessionstorage_get sessionstorage_list snapshot start_recording start_tracing start_video stop_recording
    stop_tracing stop_video storage_state take_screenshot verify_element_visible verify_list_visible verify_text_visible
    verify_value video_chapter video_hide_actions video_show_actions wait_for webmcp_list""".split())
# Arguments that describe an element in words, and ones that name it by a snapshot ref (e23, f1e3), which change
# when the page renders again. A target given as a selector is kept: another selector is another element.
DESCRIPTIONS = ("element", "startElement", "endElement")  # and a form field's `name`, in `fields`
TARGETS = ("ref", "target", "startTarget", "endTarget")
REF_ID = re.compile(r"(?:f[0-9]+)?e[0-9]+")
PAGE_URL = re.compile(r"^- Page URL: (?P<url>\S+)", re.MULTILINE)
# What a call's Result says it logged to the console (its `events`): a console log of the evidence and its lines.
CONSOLE_LINK = re.compile(r"New console entries: (?P<path>\S+?)#L(?P<first>[0-9]+)(?:-L(?P<last>[0-9]+))?")
ARGS = re.compile(r"```json\n(?P<json>.*?)\n```", re.DOTALL)


def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def session_calls(root):
    """[(tool name, its Args as written, its Result)] per session log, in the order called."""
    sessions = []
    for session in sorted(glob.glob(os.path.join(root, "session-*", "session.md"))):
        calls, listed = TOOL_CALL.split(read(session)), []
        for name, body in zip(calls[1::2], calls[2::2]):
            block = ARGS.search(body)  # the Args: the first fenced block (JSON holds no raw newline)
            listed.append((name, block.group("json"), body[block.end():]) if block else (name, "", body))
        sessions.append(listed)
    return sessions


def tool_calls(root):
    """[(tool name, the file its Args saved to or '', (URL, its other arguments) from inputs, its Result)] per session
    log, in the order called."""
    return [[(name, saved_to(args), inputs(args), result) for name, args, result in session]
            for session in session_calls(root)]


def saved_to(args):
    saved = SAVED_TO.search(args)
    return saved.group("name") if saved else ""


def evidence_path(root, name):
    """A file a tool was told to write (`name`, from the workspace: qae-artifacts/qae/AC1-network.txt) as a path in the
    evidence directory `root`, which is all the verify job holds, or None outside it."""
    evidence = os.path.basename(os.path.normpath(root)) + "/"
    path = os.path.normpath(os.path.join(root, name.rpartition(evidence)[2]))
    return path if path.startswith(os.path.join(os.path.normpath(root), "")) else None


def unref(value, field=False):
    """`value` without its snapshot refs and descriptions in words; `field` is one of browser_fill_form's `fields`,
    whose `name` is a description too (another tool's `name`, a cookie's or a WebMCP tool's, is an argument)."""
    if isinstance(value, dict):
        return {key: unref(item, key == "fields") for key, item in value.items()
                if key not in DESCRIPTIONS and not (field and key == "name")
                and not (key in TARGETS and REF_ID.fullmatch(str(item)))}
    return [unref(item, field) for item in value] if isinstance(value, list) else value


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
    """The calls that changed the page, as (tool, (URL, other arguments)) in order: what a step did, reads left out."""
    return [(name, given) for name, _, given, _ in calls if name.removeprefix("browser_") not in READS]


def screenshots(session, item, k, m):
    """(the screenshot before step k's of any step or -1, step k's first and last, step m's first and last) as call
    indexes, or None when the session holds no screenshot of step k before one of step m. A step may be shot more
    than once."""
    shots = [i for i, call in enumerate(session) if call[0] == "browser_take_screenshot"]
    after = [i for i in shots if session[i][1].endswith("qae/%s-step-%s.png" % (item, m))]
    own = [i for i in shots if after and i < after[0] and session[i][1].endswith("qae/%s-step-%s.png" % (item, k))]
    if not own:
        return None
    return max([i for i in shots if i < own[0]], default=-1), own[0], own[-1], after[0], after[-1]


def result_field(result, field):
    """One field of a call's Result as playwright-mcp writes it (`page`, `result`), or '': never the rest of it, which
    holds what the page shows (its title, a script's return value)."""
    block = ARGS.search(result)
    try:
        value = json.loads(block.group("json")).get(field)
    except (AttributeError, ValueError):
        return ""
    return value if isinstance(value, str) else ""


def page_urls(result):
    """The URLs a call's Result says the page was at: its `page` field, which playwright-mcp writes, never the result
    a script returned."""
    return PAGE_URL.findall(result_field(result, "page"))


def snapshot(result):
    """The page's snapshot playwright-mcp wrote in a call's Result (`snapshot`, `inlineSnapshot` for browser_snapshot),
    or '': never a script's result, which holds whatever the script returned."""
    return result_field(result, "snapshot") or result_field(result, "inlineSnapshot")


def console_lines(root, result):
    """(console log path in `root`, line number) per line of the console entries a call's Result says it logged."""
    lines = []
    for link in CONSOLE_LINK.finditer(result_field(result, "events")):
        path = evidence_path(root, link.group("path"))
        if path and os.path.isfile(path):
            first = int(link.group("first"))
            lines += [(os.path.relpath(path, root), n) for n in range(first, int(link.group("last") or first) + 1)]
    return lines


def network_texts(root, calls):
    """The run's network records: each browser_network_requests result in the session logs (`calls`, from tool_calls)
    and the file it saved, and network-*.log. Never another call's arguments or result, a step log or a verdict, which
    hold text the explorer chose."""
    texts = [read(path) for path in glob.glob(os.path.join(root, "network-*.log"))]
    for name, saved, _, result in (call for session in calls for call in session):
        if name != "browser_network_requests":
            continue
        texts.append(result_field(result, "result"))
        path = evidence_path(root, saved) if saved else None
        if path and os.path.isfile(path):
            texts.append(read(path))
    return texts
