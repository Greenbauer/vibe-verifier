"""The parsers of a tool descriptor's report (gates/_descriptors.py has the descriptor format): each reads the tool's
stdout into Findings, and raises ValueError, TypeError, KeyError, AttributeError or IndexError on a report it cannot
read, which the runner turns into "could not run" (or a CannotRun of its own, saying why). A tool whose report none
reads gets a named parser here. A parser that names each finding's rule sets `rule` on it, for the count ratchet
(gates/_ratchet.py)."""
import json
import os
import urllib.parse

from _contract import CannotRun, Finding


class _Fields(dict):
    def __missing__(self, key):
        return "?"


def _line(value):
    return value if isinstance(value, int) and value > 0 else None


def json_records(text, output, repo):
    records = json.loads(text) if text.strip() else None
    if records is None:
        return []
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError("the report is not a list of objects")
    findings = []
    for record in records:
        finding = Finding(output["message"].format_map(_Fields(record)), record.get(output["path"]) or None,
                          _line(record.get(output["line"])))
        finding.rule = record.get(output["rule"]) if "rule" in output else None
        findings.append(finding)
    return findings


def _sarif_path(uri, repo):
    """A SARIF artifact URI as a path, percent-decoded; a file URI is absolute, and repository_path reads it."""
    return urllib.parse.unquote(urllib.parse.urlparse(uri).path) if uri else None


def _sarif_finding(result, repo):
    location = ((result.get("locations") or [{}])[0] or {}).get("physicalLocation") or {}
    rule, message = result.get("ruleId"), (result.get("message") or {}).get("text") or "?"
    finding = Finding("%s: %s" % (rule, message) if rule else message,
                      _sarif_path((location.get("artifactLocation") or {}).get("uri"), repo),
                      _line((location.get("region") or {}).get("startLine")), advisory=result.get("level") in ("note", "none"))
    finding.rule = rule
    return finding


def sarif(text, output, repo):
    report = json.loads(text) if text.strip() else {"runs": []}
    return [_sarif_finding(result, repo) for run in report["runs"] for result in run.get("results") or []
            if not result.get("suppressions")]


def _zizmor_finding(item):
    """The Finding for one item of zizmor's JSON report, placed at its primary location."""
    locations = item.get("locations") or [{}]
    primary = next((loc for loc in locations if loc.get("symbolic", {}).get("kind") == "Primary"), locations[0])
    symbolic, concrete = primary.get("symbolic", {}), primary.get("concrete", {})
    path = symbolic.get("key", {}).get("Local", {}).get("verbatim_path")
    row = concrete.get("location", {}).get("start_point", {}).get("row")
    judged = item.get("determinations", {})
    message = "%s: %s (%s severity, %s confidence) %s" % (
        item.get("ident", "?"), symbolic.get("annotation") or item.get("desc", ""),
        str(judged.get("severity", "?")).lower(), str(judged.get("confidence", "?")).lower(), item.get("url", ""))
    finding = Finding(message.rstrip(), path or None, row + 1 if isinstance(row, int) else None)
    finding.rule = item.get("ident")
    return finding


def zizmor(text, output, repo):
    return [_zizmor_finding(item) for item in (json.loads(text) if text.strip() else []) if not item.get("ignored")]


def bandit(text, output, repo):
    """Bandit's JSON report: each result, its rule the Bandit check (B602); a file Bandit could not read is no verdict."""
    report = json.loads(text)
    if report.get("errors"):
        first = report["errors"][0]
        raise CannotRun("bandit could not read %s: %s" % (os.path.normpath(first.get("filename", "?")), first.get("reason", "")))
    findings = []
    for item in report.get("results", []):
        finding = Finding("%s %s (severity %s, confidence %s)" % (item["test_id"], item["issue_text"],
                                                                 item["issue_severity"].lower(), item["issue_confidence"].lower()),
                          os.path.normpath(item["filename"]), item["line_number"])
        finding.rule = item["test_id"]
        findings.append(finding)
    return findings


def eslint(text, output, repo):
    """ESLint's JSON report (`--format json`): a list of files, each with its messages. An error (severity 2) blocks and a
    warning (1) is advisory, as ESLint itself fails only on errors. A message with no rule is ESLint's own: a file it
    could not parse (marked fatal, an error) under the rule "parse", anything else (an unused eslint-disable comment, an
    ignored file) under "eslint"."""
    findings = []
    for result in json.loads(text) if text.strip() else []:
        for message in result["messages"]:
            rule = message.get("ruleId") or ("parse" if message.get("fatal") else "eslint")
            finding = Finding("%s: %s" % (rule, message["message"]), result["filePath"], _line(message.get("line")),
                              advisory=message.get("severity") != 2)
            finding.rule = rule
            findings.append(finding)
    return findings


PARSERS = {"json": json_records, "sarif": sarif, "zizmor": zizmor, "bandit": bandit, "eslint": eslint}
