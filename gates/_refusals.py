"""The refusals a QAE run declares expected (`expected-refusal: <status> <path-or-URL>`), as qae-artifacts reads them:
from every source gates/_qae_rules.py DECLARED_BY names, each counting for every criterion of the run (qae_artifacts.py,
rule 5). Split from qae_artifacts.py, which reads the rest of the run's evidence."""
import os
import re
import urllib.parse

from _contract import Finding
from _features import parse, selection
from _qae_rules import DECLARED_BY, REFUSABLE
from _retry import exact

REFUSAL = re.compile(r"expected-refusal:[ \t]*(?P<status>[^\s`]+)[ \t]+(?P<target>[^\s`)]+)", re.IGNORECASE)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def refusal_pattern(target):
    """The exact URL a declaration names; each `{id}` is one path segment a run mints (no `/`, `?`, `#`)."""
    return re.compile("[^/?#]+".join(re.escape(part) for part in exact(target).split("{id}")))


def refusal_expected(expected, status, url):
    return any(status == want and pattern.fullmatch(exact(url)) for want, pattern in expected)


def expected_refusals(items, sites):
    """The (status, URL pattern) pairs the criteria `items` ((id, wording) pairs) declare expected, and findings
    for any declaration that cannot be honoured as written."""
    expected, findings = set(), []
    for ac, wording in items:
        for match in REFUSAL.finditer(wording):
            status, target = match.group("status"), match.group("target").rstrip(".,;:")
            if status not in REFUSABLE:
                findings.append(Finding("%s declares expected-refusal %s: only a handled refusal (%s) can be expected"
                                        % (ac, status, ", ".join(REFUSABLE))))
            elif urllib.parse.urlsplit(target).scheme in ("http", "https"):
                expected.add((status, refusal_pattern(target)))
            elif target.startswith("/") and sites:
                expected.add((status, refusal_pattern(urllib.parse.urljoin(sites[0], target))))
                expected.update((status, refusal_pattern(origin.rstrip("/") + target)) for origin in sites[1:])
            else:
                findings.append(Finding("%s declares expected-refusal at %s: write a path starting with / (resolved against "
                                        "the site under test, which needs --site or --site-file) or an http(s) URL" % (ac, target)))
    return expected, findings


def feature_steps(args):
    """(label, line) for each numbered Verify step of every feature the re-walk selected, as the pull
    request's copy of its file has it with HTML comments removed: the steps the explorer was handed."""
    if not args.features:
        return []
    chosen, features = selection(args.repo, args, args.base_ref)
    steps = []
    for fid, _ in chosen.selected:
        path = features[fid].path
        feature = parse(HTML_COMMENT.sub("", read(os.path.join(args.repo, path))), fid, path)
        steps += [("%s step %s" % (path, number), line) for number, line in feature.steps]
    return steps


def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def declared_refusals(args, criteria, ticket, sites):
    """expected_refusals over the run's every source: the pull request's `criteria`, the `ticket`'s and, with
    --features, the selected features' Verify steps."""
    sources = {"pull request": criteria, "ticket": ticket, "features": feature_steps(args)}
    # A source DECLARED_BY does not name is a KeyError: the explorer prompt names every one (gates/_qae_prompt.py).
    return expected_refusals([item for source, found in sources.items() if DECLARED_BY[source] for item in found], sites)
