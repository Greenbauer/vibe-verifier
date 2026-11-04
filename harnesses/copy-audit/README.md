# Copy audit: a model grades the UI's copy for plain English, and a gate checks its quotes

`ui-audit` measures copy by counting: a wall of text, a very long sentence, a raw value such as `in_review`.
Counting passes copy a person still stumbles on. A consumer's add-user dialog said "They get the portal's invite
email and join Acme as User when they set a password. The link lasts 24 hours; you can resend it." Every sentence
is short, and it still opens with a mechanism, shows a stored role name and chains two facts with a semicolon.

The copy audit reads it the way a person would, and holds that reading to the record:

1. **Record.** The QAE browser hook (`actions/qae-browser/ui-audit.js`, already loaded in both lanes for
   `ui-audit`) also writes `copy.jsonl`: every sentence and label each page shows, once per page and text
   ([`copy-page.js`](../../actions/qae-browser/copy-page.js) says what a block is). It reads after each load
   and navigation, and half a second after a dialog, alert or status message appears, since a dialog opens
   with no load.
2. **Grade.** `bin/vibe-verifier copy-audit --grade-only` ([`gates/_copy_audit.py`](../../gates/_copy_audit.py))
   gives a model on the Codex CLI each distinct block's ID, kind and text, and nothing else: an empty directory,
   no page, no repository, no pull request. Its prompt ([`prompt.md`](prompt.md)) and output schema
   ([`schema.json`](schema.json)) are the only copies. For each block it answers PASS, or FAIL with the rule it
   breaks most, the exact sentence or phrase that breaks it, and the whole block rewritten. It runs on the proof
   audit's Codex runner code (tools/codex's lockfile, a ChatGPT-managed login, the user's config ignored), in
   shards of at most 120 blocks, and writes `copy-grades.json` beside the record.
3. **Judge.** The [`copy-audit`](../../gates/copy_audit.py) gate, deterministic, checks that every block on the
   declared site was graded in a shard that ran, and that each FAIL's quote is in that block's text as the page
   showed it (a block the model graded twice fails when either grade does). A FAIL whose quote is not there is dropped, and the gate says how many. A counted FAIL
   blocks only when the pull request adds that copy (four consecutive whole words of it on an added line of the
   diff, or a shorter label whole, as a string or an element's text);
   otherwise it is listed as existing copy, without blocking.

The rules, in the prompt's words: answer first, one to three short sentences, everyday words, and no words from
the system's insides (a stored field, status or role name, an internal system name, an API, a sync, an ID the
person never typed). A value the application shows rather than wrote, and standard interface words, pass.

## In a QAE workflow

`harnesses/qae/explore-codex.yml` has the step, between the explorer and the write-scope check, on the runner
that already holds the explorer's Codex login:

```yaml
      - name: Grade the pages' copy
        if: ${{ !cancelled() }}
        uses: Greenbauer/vibe-verifier/actions/copy-audit@<sha>
```

With several explorers each grades its own record; the evidence step moves both files under `shard-<n>/`, and the
gate judges each record with its own grades. The Claude lane (`explore.yml`) has no Codex login on its runner: add
the same step there only on a runner that has one. Then the QAE manifest line, after `ui-audit`:

    copy-audit --artifacts qae-artifacts --site-file qae-inputs/site-url --soak

A shard the model did not grade (a usage limit, a timeout, output that is not the schema) is recorded with the
CLI's own error. The step only warns, so the walk's check is not turned red by a grade and `actions/qa-limit`
never reads it as the explorer's failure; the gate then cannot run, in soak too.

## Before a push

```bash
node actions/qae-browser/ui-audit.js --storage-state state.json --copy out/copy.jsonl https://preview.example/users
bin/vibe-verifier copy-audit --artifacts out --repo ../app --base-ref main
```

The first command also prints the layout findings. The second grades `out/copy.jsonl` with the login in
`$CODEX_HOME` (default `~/.codex`) and exits as the gate does. `--all` makes every failing block block: the list a
cleanup pull request works from.

## Changing the prompt

The fixtures `tests/fixtures/copy-audit/bad.html` (one block per rule, plus text the record must leave out) and
`good.html` (the same screens written plainly) are graded by the real model in `tests/test_copy_audit.py`'s
`Model` test, which needs a Codex login and so runs only on a laptop:

```bash
QAE_MCP=$(python3 bin/vibe-verifier tool qae-browser)/node_modules/@playwright/mcp/cli.js QAE_CHANNEL=chrome \
  COPY_AUDIT_CODEX_HOME=~/.codex python3 -I -m unittest discover -s tests -p test_copy_audit.py -k Model
```

Every bad block must fail and every good one pass. Run it three times: a rule a model reads two ways shows up as a
block that flips.
