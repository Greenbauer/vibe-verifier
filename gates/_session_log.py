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
DESCRIPTIONS = ("element", "startElement", "endElement", "name")
TARGETS = ("ref", "target", "startTarget", "endTarget")
REF_ID = re.compile(r"(?:f[0-9]+)?e[0-9]+")
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
        return {key: unref(field) for key, field in value.items()
                if key not in DESCRIPTIONS and not (key in TARGETS and REF_ID.fullmatch(str(field)))}
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
