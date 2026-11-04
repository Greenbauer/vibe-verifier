"""The facts the QAE gates judge by as data that the explorer must be told: the one place each is written.

The gates read them here, and gates/_qae_prompt.py renders the explorer prompt's rule paragraphs from them, so a
gate that changes one changes what the explorer is told (#280: the gate read expected refusals from the whole run
while the prompt still said "the criterion's own text").
"""

# Statuses a criterion may declare as an expected refusal (`expected-refusal: <status> <path>`), qae-artifacts.
REFUSABLE = ("400", "401", "403", "404", "409", "422")

# Where qae-artifacts reads those declarations, each counted for every criterion of the run: the key is the source
# check() gathers, the value what the explorer is told it is. A source check() gathers with no entry here is a
# KeyError, so a new one cannot reach the gate without reaching the prompt.
DECLARED_BY = {
    "pull request": "the pull request's criteria",
    "ticket": "the ticket's criteria",
    "features": "a re-walked feature's Verify steps",
}

# Statuses the invalid-input probe step may be answered with and still be a handled refusal (gates/_probe.py).
PROBE_STATUSES = ("400", "422")

# What a manifest's qae-artifacts line may declare environmental: (the qae-inputs/environmental line kind, the
# gate option that declares it, what its pattern matches). actions/qae-inputs writes one `<kind>: <pattern>` line
# per pattern (bin/cli/criteria.py).
ENVIRONMENTAL = (
    ("console", "--allow-console", "a console message"),
    ("request", "--allow-request", "a request whose URL"),
)

# How many Verify states of each re-walked feature the explorer walks when the manifest sets no --max-states.
MAX_STATES = 3
