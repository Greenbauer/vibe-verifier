"""How many walks a pull request's criteria are, and how they are shared across QAE explorers that run in parallel:
`criteria --max-shards` plans the explorers, and `qae-inputs` gives each its share, turn cap and tag."""
import re

from _acceptance import annotations

# The Claude lane's turn cap (claude-code-action's --max-turns), sized to what the explorer walks: one walk is
# one criterion as one role it names, or up to three states of one feature's re-walk (three is the default
# --max-states; more states add walks in proportion, which is an assumption: no run has measured the turns one
# state takes). The declared widths add none: the harness captures every screenshot at the other widths itself
# (actions/qae-browser/widths.js), and the explorer walks at the browser's own width (a width a criterion names
# itself is part of that criterion's walk). A fixed 80 cut off a correct run on a consumer (2026-10-07, under
# the old prompt, which walked each criterion at every width: two criteria at two widths took 54 turns, then
# more than 80 on a re-run of the same head). Its other runs took 18 and 34 turns for two criteria at one width,
# 39 for three, 44 for one criterion at two widths with a form, and 60 for two criteria at two widths. 40 plus
# 30 a walk was at least twice each of those, counting each width as a walk. 80 stays the floor and 240 bounds
# it: at the slowest turn measured there (4.5 seconds) 240 turns end inside the explore job's 30 minutes.
TURNS_BASE, TURNS_PER_WALK, TURNS_FLOOR, TURNS_CEILING = 40, 30, 80, 240
STATES_PER_WALK = 3
# The walks one explorer is sized for: what the turn cap reaches before its ceiling, six. A consumer's pull
# request listed 13 broad criteria at two widths for one explorer, under the old prompt that walked them all: it
# made one shallow pass, never signed in as two of the three roles it had logins for, stopped after 9 of its 30
# minutes and FAILed 9 criteria as not completed. So the criteria of a pull request with more walks than this can
# be split across explorers that run in parallel (`criteria --max-shards`), each sized for this many.
SHARD_WALKS = (TURNS_CEILING - TURNS_BASE) // TURNS_PER_WALK
SHARE = ("Your share of the criteria is %s: you are explorer %d of %d. Walk only those, and write a verdict line for "
         "each of them and for no other criterion; the other criteria are walked by other explorers in parallel, "
         "on the same site and its data.")
# The first explorer when the feature re-walk is its whole load.
NO_SHARE = ("None of the criteria is yours: you are explorer %d of %d, and other explorers walk them in parallel, "
            "on the same site and its data. Walk no criterion and write no acceptance-check line. Re-walk the "
            "features as described below, and write only their lines in the verdict.")
# Explorers of one run share the preview and its data. A pull request's explorer once opened a record another
# explorer of the same run had created, from a list both could see, while that one still had a later criterion's
# change on it, and failed a correct criterion on what it saw. So each of several explorers is given a tag, the
# run, its attempt and the explorer's number, to name what it creates by and to recognise its own records by.
ISOLATION = ("Your tag is %s. Other explorers create records on this site at the same time and change them while "
             "you look: never open, change or judge a record another explorer made. Every record you create (a "
             "profile, a quote, an order, anything with a name or a title) carries your tag: where a criterion gives "
             "the name, write that name followed by a space and %s, and it is still the name the criterion means. "
             "Open, change, snapshot or judge only records that carry your tag, or records a criterion names that "
             "existed before this run (a seeded customer, a fixture). Where a criterion would have you pick any "
             "record from a list, pick one with your tag or one no explorer made; never one whose name ends in "
             "another explorer's tag (qae-<run>-e<number>). When a criterion of yours builds on a record that a "
             "criterion outside your share creates, create your own first, by that criterion's steps, with your tag. "
             "The browser holds you to it: it hides every record that carries another explorer's tag, answers 409 "
             "itself to any request on one, and to deleting a record a criterion outside your share names, and the "
             "gate fails the run on each of those and on any record you open that another explorer created. A 409 "
             "whose error starts \"Vibe Verifier QAE\" is the harness, never the site: never judge a criterion on it.")
# A record id as a criterion names it, the same shapes actions/qae-browser/console-pages.js reads in a path: a UUID,
# 24 hex digits (a MongoDB ObjectId) or a ULID.
RECORD_ID = re.compile(r"(?<![0-9A-Za-z-])(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                       r"|[0-9a-fA-F]{24}|[0-9A-HJKMNP-TV-Z]{26})(?![0-9A-Za-z-])")
# Criteria that build on each other, which one explorer walks in this order.
CHAINED = ("%s name each other by number, so they may build on each other: walk them in that order, and where a later "
           "one works on a record an earlier one made, use that record.")
# A criterion that names another of its list by number builds on that one's state ("on the profile from item 2",
# "after AC3", "the snapshot of criteria 4 and 5"): an item or criterion number counts in its own list (the pull
# request's AC, the ticket's TC), and ACn or TCn names one outright. Such criteria are never split across
# explorers: the one that walks the later criterion would find another explorer's record, mid-change.
CRITERION_NAMED = re.compile(r"\b(?:(?i:items?|criteri(?:on|a))[ \t]+(?P<numbers>#?\d+(?:[ \t]*(?:,|and|or|&)[ \t]*#?\d+)*)"
                             r"|(?P<prefix>AC|TC)(?P<number>\d+))\b")


def criterion_walks(wording):
    """How many walks one criterion is: one as each role it names."""
    return max(1, len(annotations(wording).roles))


def rewalk_walks(features, states):
    """How many walks the re-walk of `features` features is: one for each three states the explorer walks of one."""
    return features * -(-states // STATES_PER_WALK)


def explorer_turns(items, features, states):
    """(walks, turn cap) for the criteria `items` ((id, wording) pairs), plus `features` re-walks of at most
    `states` states each."""
    walks = sum(criterion_walks(wording) for _, wording in items) + rewalk_walks(features, states)
    return walks, min(TURNS_CEILING, max(TURNS_FLOOR, TURNS_BASE + TURNS_PER_WALK * walks))


def named_criteria(item, wording):
    """The ids of the criteria `wording` (criterion `item`'s) names by number (CRITERION_NAMED), in order."""
    own = item[:2]
    named = []
    for match in CRITERION_NAMED.finditer(wording):
        named += ([match.group("prefix") + match.group("number")] if match.group("prefix")
                  else [own + number for number in re.findall(r"\d+", match.group("numbers"))])
    return named


def criterion_chains(items):
    """The criteria `items` ((id, wording) pairs) in groups that one explorer walks whole, in document order: a
    criterion that names another of `items` by number (CRITERION_NAMED) is in that one's group, and so is every
    criterion either names in turn. A criterion that names none is a group of its own. Each group keeps
    document order, and the groups are ordered by their first criterion."""
    group = {item: index for index, (item, _) in enumerate(items)}
    for item, wording in items:
        for other in named_criteria(item, wording):
            if other in group and group[other] != group[item]:
                group = joined_groups(group, other, item)
    chains = {}
    for item in items:
        chains.setdefault(group[item[0]], []).append(item)
    return [chains[key] for key in sorted(chains)]


def joined_groups(group, other, item):
    """`group` with the groups of criteria `other` and `item` made one, numbered as the earlier of the two."""
    joined, into = sorted((group[other], group[item]), reverse=True)
    return {key: into if value == joined else value for key, value in group.items()}


def chain_walks(chain):
    return sum(criterion_walks(wording) for _, wording in chain)


def shard_shares(items, shards, rewalk=0):
    """Each of `shards` explorers' share of the criteria `items`, in shard order. A chain of criteria that build on
    each other (criterion_chains) goes whole to one explorer, as a lone criterion does. Largest walks first, each
    goes to the explorer with the fewest walks so far, the lowest-numbered on a tie. The first starts with `rewalk`
    walks, those of the feature re-walk only it does: a selected feature with no walked step is a finding, so the
    explorer that carries the re-walk takes fewer criteria and reaches it in its time. Every explore job makes this
    assignment for itself, from the same inputs, so they agree. Equal-sized chains and each final share keep
    document order, without changing criterion numbers."""
    shares, loads = [[] for _ in range(shards)], [rewalk] + [0] * (shards - 1)
    positions = {item[0]: index for index, item in enumerate(items)}
    for chain in sorted(criterion_chains(items), key=chain_walks, reverse=True):
        least = loads.index(min(loads))
        shares[least].extend(chain)
        loads[least] += chain_walks(chain)
    for share in shares:
        share.sort(key=lambda item: positions[item[0]])
    return shares


def shard_plan(items, max_shards):
    """What `criteria --max-shards` prints after the criteria. `shards: [1,2,3]` numbers the explorers, for the
    explore job's matrix: enough for SHARD_WALKS walks each, never more than `max_shards` nor than the chains of
    criteria (a criterion, or a chain of criteria that build on each other, is never split), and at least one.
    Then the walks it counted, each chain, and a warning when the explorer with the most walks has more than it is
    sized for, so an oversized pull request shows up front. The count is of the criteria alone: which features
    are re-walked is selected in the explore job, from a checkout this job does not have."""
    walks = sum(criterion_walks(wording) for _, wording in items)
    chains = criterion_chains(items)
    shards = max(1, min(max_shards, len(chains), -(-walks // SHARD_WALKS)))
    lines = ["shards: [%s]" % ",".join(str(number) for number in range(1, shards + 1)), "walks: %d" % walks]
    lines += ["chain: %s (%d walks, one explorer)" % (", ".join(item for item, _ in chain), chain_walks(chain))
              for chain in chains if len(chain) > 1]
    most = max([chain_walks(share) for share in shard_shares(items, shards)] or [0])
    if most > SHARD_WALKS:
        lines.append("::warning title=vibe-verifier criteria::%d walks (each criterion once per role) for %d "
                     "explorer%s sized for %d, and one of them walks %d: expect criteria left unfinished. List fewer "
                     "criteria on one pull request, chain fewer of them, or raise max-shards (harnesses/qae/README.md)."
                     % (walks, shards, "" if shards == 1 else "s", shards * SHARD_WALKS, most))
    return lines


def explorer_share(items, shard, shards, rewalk, tag=None):
    """(the criteria explorer `shard` of `shards` walks, the sentences its prompt names them in). One explorer
    walks them all and is told nothing. One of several is told its share by code, never left to work it out, the
    criteria of it that build on each other, and its tag (`tag`, or one made of `shard` alone), which keeps it
    off the records the others create. `rewalk` is the walks of the feature re-walk, the first explorer's alone."""
    if shards == 1:
        return items, ""
    share = shard_shares(items, shards, rewalk)[shard - 1]
    tag = tag or "qae-e%d" % shard
    sentences = [SHARE % (", ".join(item for item, _ in share), shard, shards) if share else NO_SHARE % (shard, shards)]
    sentences += [CHAINED % ", ".join(item for item, _ in chain) for chain in criterion_chains(share) if len(chain) > 1]
    sentences.append(ISOLATION % (tag, tag))
    return share, " ".join(sentences)


def explorer_isolation(items, share, tag):
    """What the browser of one explorer of several holds it to (actions/qae-browser/console-pages.js): its tag, and
    the records it may not delete, every id a criterion outside its `share` names (a fixture another explorer's
    criterion uses), lower-cased as a UUID is matched."""
    mine = {item for item, _ in share}
    named = {match.group(0).lower() for item, wording in items if item not in mine for match in RECORD_ID.finditer(wording)}
    return {"tag": tag, "protected": sorted(named)}


def explorer_tag(run, shard):
    """The tag explorer `shard` names its records by: the run (`<run id>.<attempt>`, so a re-run's explorer does
    not take an earlier attempt's records for its own) and its number. None without a run."""
    return "qae-%s-e%d" % (run, shard) if run else None
