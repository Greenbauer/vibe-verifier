"""`qae-inputs`: what the QAE explorer and its gates read from the site step's supplies and the QAE manifest, written
before the explorer runs: design references and their digests, widths, environmental patterns and, asked, the
explorer's turn cap, share and isolation (the usage text in bin/vibe-verifier says how)."""
import hashlib
import json
import os
import shutil

from _acceptance import REFERENCE_KEY, body_criteria as read_criteria, declares_none, png_size, split_ticket, ticket_items
from _qae_rules import MAX_STATES
from cli.criteria import manifest_environmental, manifest_widths
from cli.features import selection_options
from cli.qae_shards import explorer_isolation, explorer_share, explorer_tag, explorer_turns, rewalk_walks
from cli.runner import fail, read_text


def qae_inputs_command(args):
    """Copy each supplied design reference into the evidence, list it for the explorer, declare its digest,
    write the widths the manifest's qae-artifacts line requires and, asked, the explorer's turn cap and the
    sentence that names its share of the criteria."""
    if not 1 <= args.shard <= args.shards:
        return fail("--shard %d is not one of --shards %d" % (args.shard, args.shards))
    widths = manifest_widths(args)
    environmental = manifest_environmental(args)
    items, turns = None, None
    if args.turns:
        items, turns, code = explorer_inputs(args, widths)
        if code is not None:
            return code
    write_lines(args.widths_file, ["%d" % width for width in widths])
    write_lines(args.environmental_file, environmental)
    found, code = supplied_references(args)
    if code is not None:
        return code
    digests = copy_references(args, found)
    if args.digests:
        with open(args.digests, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(digests, sort_keys=True) + "\n")
    print("\n".join(["references: %d" % len(found)] + ["%s: %dx%d" % (key, *size) for key, (_, size) in sorted(found.items())]
                    + ["widths: %s" % (", ".join(map(str, widths)) or "none")]
                    + ["environmental: %d" % len(environmental)]
                    + (["max turns: %d (%d walks)" % (turns[1], turns[0])] if turns else [])
                    + (["share: %s (explorer %d of %d)" % (", ".join(item for item, _ in items) or "no criterion",
                                                           args.shard, args.shards)] if turns and args.shards > 1 else [])))
    return 0


def explorer_inputs(args, widths):
    """(the criteria this explorer walks, (walks, turn cap), None) after writing its turn cap, share and isolation;
    (None, None, exit code) when the criteria cannot be read or it has nothing to walk."""
    try:
        criteria_text = read_text(args.criteria)
        ticket_text = read_text(args.ticket) if os.path.isfile(args.ticket) else ""
    except OSError as error:
        return None, None, fail("could not read %s" % error)
    items = (([] if declares_none(criteria_text) else read_criteria(criteria_text))
             + split_ticket(ticket_items(ticket_text)[0], criteria_text)[0])
    names = os.listdir(args.features) if os.path.isdir(args.features) else []
    features = len([name for name in names if name.endswith(".md")])
    options = selection_options(args) if args.manifest and features else None
    states = options.max_states if options else MAX_STATES
    tag = explorer_tag(args.run, args.shard) or "qae-e%d" % args.shard
    everything = items
    items, share = explorer_share(items, widths, args.shard, args.shards, rewalk_walks(features, states), tag)
    write_isolation(args, everything, items, tag)
    if args.shard > 1:
        # Every explorer counted the features, to share the criteria out alike. Only the first re-walks them.
        features = 0
        shutil.rmtree(args.features, ignore_errors=True)
        if os.path.isfile(args.features.rstrip("/") + ".md"):
            os.remove(args.features.rstrip("/") + ".md")
    if not items and not features and args.shards > 1:
        # The criteria job counted more criteria than this job reads: the body was edited between them.
        return None, None, fail("explorer %d of %d has no criterion to walk: the pull request's criteria changed after "
                                "the criteria job counted them, so run the workflow again" % (args.shard, args.shards))
    turns = explorer_turns(items, widths, features, states)
    write_share(args, turns, share, items)
    return items, turns, None


def write_isolation(args, everything, items, tag):
    """What the browser holds one explorer of several to, at --isolation."""
    if not args.isolation:
        return
    # One explorer has the site to itself: no file, and the browser guards nothing.
    if os.path.isfile(args.isolation):
        os.remove(args.isolation)
    if args.shards > 1:
        os.makedirs(os.path.dirname(args.isolation) or ".", exist_ok=True)
        with open(args.isolation, "w", encoding="utf-8") as handle:
            json.dump(explorer_isolation(everything, items, tag), handle)


def write_share(args, turns, share, items):
    """The turn cap at --turns, and, asked, the share's sentence and its criteria's ids."""
    with open(args.turns, "w", encoding="utf-8") as handle:
        handle.write("%d\n" % turns[1])
    if args.share:
        with open(args.share, "w", encoding="utf-8") as handle:
            handle.write(share + "\n")
    if args.share_ids:
        with open(args.share_ids, "w", encoding="utf-8") as handle:
            json.dump([item for item, _ in items], handle)


def write_lines(path, lines):
    """`lines` at `path`, one per line; no lines writes no file."""
    if lines:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines))


def supplied_references(args):
    """({key: (path, (width, height))}, None) for each <key>.png the site step wrote, or (None, exit code) for a
    file that is not a PNG named for a reference key."""
    names = sorted(os.listdir(args.references)) if os.path.isdir(args.references) else []
    found = {}
    for name in (name for name in names if name.endswith(".png")):
        path = os.path.join(args.references, name)
        size = png_size(path) if REFERENCE_KEY.match(name[:-4]) else None
        if not size:
            return None, fail("%s is not a PNG named <key>.png (a key is letters, digits, - and _)" % path)
        found[name[:-4]] = (path, size)
    return found, None


def copy_references(args, found):
    """Copy each reference into the evidence and list it for the explorer: {key: sha256}."""
    digests = {}
    if not found:
        return digests
    os.makedirs(os.path.join(args.evidence, "references"), exist_ok=True)
    listing = ["# Design references", "",
               "The workflow supplied these images. A criterion naming `[ref: <key>]` is compared with the "
               "image of that key, at its width.", ""]
    for key, (path, (width, height)) in sorted(found.items()):
        shutil.copyfile(path, os.path.join(args.evidence, "references", key + ".png"))
        with open(path, "rb") as handle:
            digests[key] = hashlib.sha256(handle.read()).hexdigest()
        listing.append("- %s: %s, %d pixels wide (%d x %d)" % (key, path, width, width, height))
    with open(args.listing, "w", encoding="utf-8") as handle:
        handle.write("\n".join(listing) + "\n")
    return digests


def add_arguments(parser):
    parser.add_argument("--references", default="qae-inputs/references", metavar="DIR",
                        help="where the site step wrote <key>.png (default: qae-inputs/references)")
    parser.add_argument("--evidence", default="qae-artifacts", metavar="DIR",
                        help="the explorer's artifact directory; copies go to its references/ (default: qae-artifacts)")
    parser.add_argument("--listing", default="qae-inputs/references.md", metavar="FILE",
                        help="the list the explorer reads (default: qae-inputs/references.md)")
    parser.add_argument("--digests", metavar="FILE", help="write {key: sha256} here, one JSON object")
    parser.add_argument("--manifest", metavar="FILE", help="the QAE manifest: its qae-artifacts line's --widths, judged "
                        "from the base, are written to --widths-file, and its --allow-console and --allow-request "
                        "patterns to --environmental-file")
    parser.add_argument("--widths-file", default="qae-inputs/widths", metavar="FILE",
                        help="where the widths go, one per line (default: qae-inputs/widths)")
    parser.add_argument("--environmental-file", default="qae-inputs/environmental", metavar="FILE",
                        help="where the qae-artifacts line's --allow-console and --allow-request patterns go, one "
                        "`console: <regex>` or `request: <regex>` line each (default: qae-inputs/environmental)")
    parser.add_argument("--turns", metavar="FILE", help="write the explorer's turn cap here, sized to its walks")
    parser.add_argument("--criteria", default="qae-inputs/pr-body.md", metavar="FILE",
                        help="with --turns, the PR body whose criteria are walked (default: qae-inputs/pr-body.md)")
    parser.add_argument("--ticket", default="qae-inputs/ticket.md", metavar="FILE",
                        help="with --turns, the ticket's criteria, when the file exists (default: qae-inputs/ticket.md)")
    parser.add_argument("--features", default="qae-inputs/features", metavar="DIR",
                        help="with --turns, the features to re-walk, one <id>.md each (default: qae-inputs/features); "
                        "counted by every explorer, and removed, with its listing DIR.md, for one past the first")
    parser.add_argument("--shard", metavar="K", type=int, default=1,
                        help="with --turns, which explorer of --shards this is: the cap is sized to its share (default: 1)")
    parser.add_argument("--shards", metavar="N", type=int, default=1,
                        help="with --turns, how many explorers the criteria are split across (default: 1)")
    parser.add_argument("--share", metavar="FILE", help="with --turns, write the sentence that names this explorer's "
                        "share of the criteria here, for its prompt (an empty line for one explorer)")
    parser.add_argument("--share-ids", metavar="FILE", help="with --turns, write the ids of the criteria this explorer "
                        "walks here, as a JSON list (every criterion for one explorer), for the step that keeps its evidence")
    parser.add_argument("--run", metavar="ID", default="", help="with --shards above 1, the run (`<run id>.<attempt>`) in "
                        "the tag each explorer names its records by, qae-<run>-e<K> (default: none, qae-e<K>)")
    parser.add_argument("--isolation", metavar="FILE", help="with --turns, write what the browser holds one explorer of "
                        "several to here, its tag and the record ids it may not delete (none, and the file removed, for one)")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base-ref", default=None)
