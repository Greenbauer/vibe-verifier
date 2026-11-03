"""`features`: the features of the map the manifest's acceptance-verdict line names that a pull request's changed
paths touch, copied for the QAE explorer to re-walk (the usage text in bin/vibe-verifier says how)."""
import argparse
import os
import shutil
import sys

from _contract import CannotRun
from _features import add_selection_arguments, describe, selection
from cli.runner import fail, judged_entries


def selection_options(args):
    """The selection options on the manifest's acceptance-verdict line, judged from the base; None when the
    manifest has no such line."""
    # The notice goes to stderr: a workflow reads the first line of `features` as `features: <n>`.
    lines = [gate_args for gate, gate_args in judged_entries(args, notice=sys.stderr) if gate == "acceptance-verdict"]
    parser = argparse.ArgumentParser(prog="acceptance-verdict", add_help=False)
    add_selection_arguments(parser)
    try:
        return parser.parse_known_args(lines[0])[0] if lines else None
    except SystemExit:
        sys.exit(fail("the acceptance-verdict line in %s has a malformed selection option" % args.manifest))


def features_listing(chosen, states, out):
    """What the explorer reads beside the feature files in `out`: how many states to walk of each, and the
    changed paths that selected each, which say where in the feature to look first."""
    lines = ["# Features to re-walk", "",
             "Each feature below is described in %s/<id>.md, as this pull request has that file. Walk at most %d "
             "state%s of each, first the states closest to the changed files listed for it."
             % (out, states, "" if states == 1 else "s"), ""]
    return "\n".join(lines + ["- %s: %s" % (fid, ", ".join(paths)) for fid, paths in chosen.selected]) + "\n"


def features_command(args):
    """The features the acceptance-verdict line's selection picks for these changed paths."""
    options = selection_options(args)
    if not options or not options.features:
        print("features: 0 (the re-walk is off: %s has no acceptance-verdict line with --features)" % args.manifest)
        return 0
    options.changed_files = args.changed_files
    try:
        chosen, features = selection(args.repo, options, args.base_ref)
    except CannotRun as error:
        return fail("features: %s" % error)
    print("\n".join(["features: %d" % len(chosen.selected)] + describe(chosen)))
    if args.out and chosen.selected:
        os.makedirs(args.out, exist_ok=True)
        for fid, _ in chosen.selected:
            shutil.copyfile(os.path.join(args.repo, features[fid].path), os.path.join(args.out, fid + ".md"))
        if args.listing:
            with open(args.listing, "w", encoding="utf-8") as handle:
                handle.write(features_listing(chosen, options.max_states, args.out))
    return 0


def add_arguments(parser):
    parser.add_argument("--manifest", default=".vibe-verifier-qae")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base-ref", default=None)
    parser.add_argument("--changed-files", required=True, metavar="FILE", help="the pull request's changed paths, one per line")
    parser.add_argument("--out", metavar="DIR", help="copy each selected feature file here as <id>.md")
    parser.add_argument("--listing", metavar="FILE", help="with --out, write the list the explorer reads here: the most "
                        "states to walk of each feature and the changed paths that selected each")
