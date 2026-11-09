"""Viewport widths in a QAE run, as the qae-artifacts gate judges them (its rule 6): each criterion's step screenshots,
with the captures the browser's page hook took of them at the run's other widths (actions/qae-browser/widths.js), must
include one of each width `--widths` declares. Split from qae_artifacts.py, which reads the rest of the run's evidence."""
import argparse
import hashlib
import json
import os
import re

from _acceptance import png_size
from _contract import Finding
from _refusals import read

STEP_LINE = re.compile(r"^[ \t]*-[ \t]+step[ \t]+(?P<k>[0-9]+):", re.IGNORECASE)


def relative(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def harness_captures(root):
    """{digest of an explorer screenshot: {width of each capture the harness took of it}}, from widths.jsonl
    (actions/qae-browser/widths.js); a capture counts only when its file is a PNG that many pixels wide."""
    captures = {}
    record = os.path.join(root, "widths.jsonl")
    for line in read(record).splitlines() if os.path.isfile(record) else []:
        try:
            entry = json.loads(line)
            shot, width, path = entry["shot"], int(entry["width"]), os.path.normpath(os.path.join(root, entry["file"]))
        except (ValueError, KeyError, TypeError):
            continue
        inside = path.startswith(os.path.join(os.path.normpath(root), ""))
        size = png_size(path) if inside and os.path.isfile(path) else None
        if size and size[0] == width:
            captures.setdefault(shot, set()).add(width)
    return captures


def screenshot_widths(root, log, captures):
    """({widths of the step screenshots of one log and of the harness's captures of them}, [findings for the
    screenshots that are not PNGs])."""
    stem, seen, findings = os.path.basename(log)[:-3], set(), []
    for k in (m.group("k") for m in map(STEP_LINE.match, read(log).splitlines()) if m):
        shot = os.path.join(root, "qae", "%s-step-%s.png" % (stem, k))
        if not os.path.isfile(shot) or os.path.getsize(shot) == 0:
            continue  # already a finding: the step has no screenshot
        size = png_size(shot)
        if size is None:
            findings.append(Finding("step %s's screenshot is not a PNG, so its width cannot be read" % k, relative(log, root)))
        else:
            seen.add(size[0])
            with open(shot, "rb") as handle:
                seen |= captures.get(hashlib.sha256(handle.read()).hexdigest(), set())
    return seen, findings


def widths_findings(root, widths, logs):
    """A finding per criterion step log whose step screenshots, with the harness's captures of them, miss a declared
    width."""
    findings, captures = [], harness_captures(root)
    for log in logs:
        seen, unreadable = screenshot_widths(root, log, captures)
        findings += unreadable
        missing = [width for width in widths if width not in seen]
        if missing:
            findings.append(Finding("no step screenshot %s pixels wide (its widths: %s): check the criterion's end state "
                                    "at each width in qae-inputs/widths" % (" or ".join(map(str, missing)),
                                                                            ", ".join(map(str, sorted(seen))) or "none"),
                                    relative(log, root)))
    return findings


def width_list(text):
    """`1280,375` as [1280, 375]: what --widths takes, also read by actions/qae-inputs from the manifest line."""
    try:
        widths = [int(part) for part in text.split(",")]
    except ValueError:
        widths = []
    if not widths or any(width < 1 for width in widths):
        raise argparse.ArgumentTypeError("widths are positive whole numbers of pixels, comma-separated: %r" % text)
    return list(dict.fromkeys(widths))
