"""Every parser of what playwright-mcp writes, fed every file the pinned build wrote in tests/fixtures/playwright-mcp/,
and held to what the pages did (its manifest.json). How the fixtures are made, and when to make them again:
tests/test_qae_recorded.py, which also runs the gate on them. No browser, no network."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts gates/ on the path)
import qae_artifacts
from _page_record import digest, page_record
from _probe import ENTRY, NUMBERED, REFUSED
from _retry import CONNECTION, REQUEST, RESOURCE, WEBSOCKET, console_errors as environment_errors
from _session_log import REF_ID, console_lines, network_texts, page_urls, result_field, session_calls, snapshot, tool_calls
from _snapshot import NODE, REF, elements
from test_qae_recorded import FIXTURES, MANIFEST

SCENARIOS = sorted(MANIFEST["scenarios"])
ORIGINS = MANIFEST["origins"]
# The two other lines a network record holds: the count of the successful static requests it left out, and the link a
# call that saved its result to a file returns instead of the requests.
NOTE = "Note: %d static request%s not shown, run with \"static\" option to see %s."
LINK = "- [Network](%s)"


COPIES = {}


def setUpModule():
    work = tempfile.mkdtemp(prefix="vv-recorded-")
    unittest.addModuleCleanup(shutil.rmtree, work, True)
    for name in SCENARIOS:
        COPIES[name] = shutil.copytree(FIXTURES / name, os.path.join(work, name, "qae-artifacts"))


def root(name):
    """Scenario `name`'s recording in a directory named qae-artifacts, as a run's evidence is: a saved file and a console
    link name their path from there (qae-artifacts/qae/AC1-network.txt)."""
    return str(COPIES[name])


def truth(name):
    return MANIFEST["scenarios"][name]


def logged(name):
    """{(kind, message)} of the distinct errors the scenario's console logs hold, as the gate reads them."""
    return {(kind, message) for log in sorted(Path(root(name)).glob("console-*.log"))
            for _, kind, message in qae_artifacts.console_errors(log.read_text())}


def where(message):
    """The URLs a console error names, read without the gate's patterns: the resource after its last ` @ ` without its
    line, then each stack frame's script with its line and column (the gate judges a URL by its start)."""
    first, _, stack = message.partition("\n")
    urls = [first.rpartition(" @ ")[2].rsplit(":", 1)[0]] if " @ " in first else []
    return urls + [frame.strip()[3:] for frame in stack.splitlines()]


class Network(unittest.TestCase):
    def test_every_line_of_every_network_record_is_a_request_the_page_made_or_says_what_it_is(self):
        for name in SCENARIOS:
            with self.subTest(name):
                calls, seen = tool_calls(root(name)), set()
                static = any(json.loads(args or "{}").get("static") for session in session_calls(root(name))
                             for _, args, _ in session)
                for text in network_texts(root(name), calls):
                    for line in filter(None, text.splitlines()):
                        request = REQUEST.match(line)
                        if not request:
                            self.assertTrue(any(line == NOTE % (n, "s" * (n > 1), "them" if n > 1 else "it") for n in range(1, 9))
                                            or any(line == LINK % saved for _, saved, _, _ in calls[0]), line)
                            continue
                        method, url, status, reason = request.group("method", "url", "status", "reason")
                        seen.add((method, url, status if status != "FAILED" else "FAILED " + reason))
                        if status != "FAILED":
                            self.assertEqual(NUMBERED.match(line).group("n", "method", "url", "status"),
                                             (line.split(".")[0], method, url, status))
                expected = {tuple(request) for request in truth(name)["requests"] + (truth(name)["hidden"] if static else [])}
                self.assertEqual(seen, expected)

    def test_a_connection_failure_is_one_the_retry_rule_reads_as_the_environments(self):
        texts = network_texts(root("errors"), tool_calls(root("errors")))
        failed = {request.group("reason") for text in texts for request in REQUEST.finditer(text) if request.group("reason")}
        self.assertEqual({reason for reason in failed if CONNECTION.fullmatch(reason)}, {"net::ERR_CONNECTION_REFUSED"})


class Console(unittest.TestCase):
    def test_every_line_is_an_entry_or_a_frame_of_the_one_before(self):
        # Entries are not in time order, and a tab a link opened logs what it did before its log was opened at a
        # negative offset.
        offsets = []
        for log in FIXTURES.glob("*/console-*.log"):
            for line in log.read_text().splitlines():
                entry = qae_artifacts.CONSOLE_ENTRY.match(line)
                self.assertTrue(entry or line.startswith("    at "), "%s: %r" % (log, line))
                self.assertEqual(bool(entry), bool(ENTRY.match(line)), line)
                offsets += [int(line[1:].split("ms]")[0])] if entry else []
        self.assertTrue(any(offset < 0 for offset in offsets), "no negative offset recorded")

    def test_the_entries_group_into_the_errors_each_page_had_and_name_what_they_are_about(self):
        for name in SCENARIOS:
            with self.subTest(name):
                found = logged(name)
                self.assertEqual(len(found), len(truth(name)["console"]), sorted(found))
                for kind, start in truth(name)["console"]:
                    matched = [message for got, message in found if got == kind and message.startswith(start)]
                    self.assertEqual(len(matched), 1, (kind, start, sorted(found)))
                    self.assertEqual(qae_artifacts.about(matched[0]), where(matched[0]))

    def test_chromium_lines_for_an_answer_match_the_patterns_that_judge_and_excuse_them(self):
        for name in SCENARIOS:
            for kind, message in logged(name):
                answered = message.startswith("Failed to load resource: the server responded with a status of ")
                with self.subTest(message=message):
                    resource = qae_artifacts.RESOURCE_ERROR.match(message)
                    self.assertEqual(bool(resource), answered)
                    if resource:
                        self.assertEqual(resource.group("status", "url"), (message.split("status of ")[1][:3], where(message)[0]))
                    refused = REFUSED.match("[ERROR] " + message)
                    self.assertEqual(bool(refused), answered and message.split("status of ")[1][:3] in ("400", "422"))

    def test_a_failed_connection_and_websocket_are_what_a_retry_is_excused_by(self):
        closed, socket = ORIGINS["closed"] + "/api/down", ORIGINS["socket"] + "/socket"
        saved = Path(root("errors"), "qae", "AC1-step-1-console.txt").read_text()
        self.assertEqual([url for _, url, _ in environment_errors(saved)], [closed, socket])
        resource = RESOURCE.search(saved)
        self.assertEqual(resource.group("reason", "url"), ("net::ERR_CONNECTION_REFUSED", closed))
        self.assertEqual(WEBSOCKET.search(saved).group("url"), socket)
        self.assertEqual(list(environment_errors(Path(root("retry"), "qae", "AC1-step-1-console.txt").read_text())), [])


class SessionLog(unittest.TestCase):
    def test_every_call_is_listed_as_made_with_what_it_saved_where_it_was_and_what_it_logged(self):
        for name in SCENARIOS:
            with self.subTest(name):
                sessions = tool_calls(root(name))
                self.assertEqual(len(sessions), 1)
                self.assertEqual([[tool, saved] for tool, saved, _, _ in sessions[0]], truth(name)["calls"])
                for tool, _, (url, _), result in sessions[0]:
                    if tool == "browser_navigate":
                        self.assertEqual(page_urls(result), [url])
                    events = result_field(result, "events")
                    if "New console entries" in events:
                        places = console_lines(root(name), result)
                        self.assertTrue(places, events)
                        lines = [Path(root(name), path).read_text().splitlines()[number - 1] for path, number in places]
                        self.assertTrue(ENTRY.match(lines[0]), lines[0])

    def test_every_snapshot_line_naming_an_element_parses(self):
        states = [snapshot(result) for name in SCENARIOS for _, _, _, result in tool_calls(root(name))[0]]
        states += [path.read_text() for path in FIXTURES.glob("*/page-*.yml")]
        refs = 0
        for state in filter(None, states):
            for line in (line for line in state.splitlines() if "[ref=" in line):
                self.assertTrue(NODE.match(line) and REF_ID.fullmatch(REF.search(line).group("ref")), line)
            self.assertEqual(len(elements(state)), state.count("[ref="), state)
            refs += state.count("[ref=")
        self.assertTrue(any("[ref=f" in state for state in states), "no frame ref recorded")
        self.assertGreater(refs, 0)

    def test_the_probes_controls_are_named_by_refs_both_snapshots_show(self):
        calls = session_calls(root("probe"))[0]
        before, after = [snapshot(result) for tool, _, result in calls if tool == "browser_snapshot"][1:]
        targets = [json.loads(args)["target"] for tool, args, _ in calls if tool in ("browser_type", "browser_click")]
        self.assertEqual([elements(before)[target] for target in targets], [("textbox", "Quantity"), ("button", "Save")])
        self.assertEqual([elements(after)[target] for target in targets], [("textbox", "Quantity"), ("button", "Save")])


class PageRecord(unittest.TestCase):
    def test_it_names_the_page_of_every_console_error_and_holds_every_answer(self):
        for name in SCENARIOS:
            with self.subTest(name):
                pages, answers = page_record(root(name))
                firsts = {digest(message.split("\n")[0]) for _, message in logged(name)}
                self.assertEqual(set(pages), firsts)
                self.assertTrue(all(set(pages[first]) == set(truth(name)["pages"]) for first in firsts), pages)
                self.assertEqual(answers, {digest(answer) for answer in truth(name)["answered"]})


if __name__ == "__main__":
    unittest.main()
