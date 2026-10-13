"""The YAML of repo-rules' rule files, their tests and each sgconfig.yml, read with ast-grep's YAML grammar."""
import json
import os
import re
import subprocess

from _contract import CannotRun

# The standard library reads no YAML, so ast-grep's own YAML grammar reads the rule files: each
# document, each top-level key of a document, and each item of a top-level sequence.
_TOP = ("utils: {top: {kind: block_mapping_pair, "
        "inside: {kind: block_mapping, inside: {kind: block_node, inside: {kind: document}}}}}")
READER = "\n---\n".join([
    "id: doc\nlanguage: yaml\nrule: {kind: document}",
    "id: key\nlanguage: yaml\n%s\nrule: {matches: top, has: {field: key, pattern: $KEY}}" % _TOP,
    "id: item\nlanguage: yaml\n%s\nrule:\n  any:\n"
    "    - {kind: block_sequence_item, inside: {kind: block_sequence, inside: {kind: block_node, inside: {matches: top}}}}\n"
    "    - {kind: flow_node, inside: {kind: flow_sequence, inside: {kind: flow_node, inside: {matches: top}}}}" % _TOP,
])


def scalar(text):
    """A YAML scalar as a rule's id or message writes it: plain, quoted, or a block."""
    text = text.strip()
    if text[:1] in ("|", ">"):
        return text.partition("\n")[2].strip()
    if len(text) > 1 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) > 1 and text[0] == text[-1] == '"':
        try:
            return json.loads(text)
        except ValueError:
            return text[1:-1]
    return "" if text in ("~", "null", "Null", "NULL") else text


def spans(hits, rule):
    """[(file, start byte, end byte, hit)] for the hits of one READER rule."""
    return [(h["file"], h["range"]["byteOffset"]["start"], h["range"]["byteOffset"]["end"], h) for h in hits if h["ruleId"] == rule]


def top_keys(hits):
    """{(file, start, end) of a top-level key: [the key, its value as written, [each item of its sequence, as a scalar]]}."""
    keys = {}
    for path, start, end, hit in spans(hits, "key"):
        label = hit["metaVariables"]["single"]["KEY"]["text"]
        keys[(path, start, end)] = [scalar(label), hit["text"][len(label):].lstrip()[1:], []]  # value: past the key's colon
    for path, offset, _, hit in spans(hits, "item"):
        for (where, start, end), entry in keys.items():
            if where == path and start <= offset < end:
                entry[2].append(scalar(re.sub(r"^-(\s|$)", "", hit["text"])))  # a block item starts with its `- `
    return keys


def documents(sg, work):
    """{path under work: [({top-level key: (value, [each item of its sequence, as a scalar])}, text)]}, one per
    YAML document. Only block mappings are read (`key: value` on its own line); a document of any other
    shape is refused."""
    result = subprocess.run([sg, "scan", "--inline-rules", READER, "--json=stream", "--", *sorted(os.listdir(work))],
                            cwd=work, capture_output=True, text=True)
    if result.returncode != 0:
        raise CannotRun("ast-grep could not read the rule files: %s" % (result.stderr.strip() or "no output"))
    hits = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    keys = top_keys(hits)
    docs = {}
    for path, start, end, hit in sorted(spans(hits, "doc"), key=lambda span: span[:3]):
        docs.setdefault(path, []).append(({key: (value, items) for (where, s, _), (key, value, items) in keys.items()
                                           if where == path and start <= s < end}, hit["text"]))
    return docs


def has_content(text):
    """Whether a YAML document holds anything but comments and document markers."""
    return any(line and not line.startswith("#") for line in (re.sub(r"^(---|\.\.\.)", "", raw).strip() for raw in text.splitlines()))


def value(keys, key):
    """A top-level key's scalar value, or "" when the document does not set it."""
    return scalar(keys[key][0]) if key in keys else ""
