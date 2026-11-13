# Reference check: a second look before a design-reference FAIL counts

A criterion that carries `[ref: <key>]` is compared with a design reference by the QAE explorer, by looking
(harnesses/qae/prompt.md). One look can invent a difference. On a consumer's run (2026-11-13) the explorer failed
a criterion with "the modal adds a close X absent from the reference", and the reference has the same X. The
same step had passed on an earlier run. Nothing checked the claim, so a correct screen blocked the pull request.

So a FAIL whose one anchor is a comparison step is put to a second model before it counts:

1. **Ask.** `bin/vibe-verifier reference-check --artifacts DIR` ([`gates/_reference_check.py`](../../gates/_reference_check.py))
   reads DIR/verdict.md. For each `acceptance-check: ACn -- FAIL -- ...` line whose one anchor is
   `qae/ACn.md::step k: compared with reference <key> -> ...`, it gives a fresh model on the Codex CLI the
   reference (`references/<key>.png`), that step's screenshot (`qae/ACn-step-k.png`) and the FAIL's own text,
   and nothing else: no explorer context, no page, no repository, an empty directory. Its prompt
   ([`prompt.md`](prompt.md)) and output schema ([`schema.json`](schema.json)) are the only copies. It answers
   `confirmed` (is a difference the claim names really there?), `other` (does the claim also say something the
   two images cannot settle, such as a step it could not check or a request that failed?) and a reason. It runs on the proof audit's Codex
   runner code (tools/codex's lockfile, a ChatGPT-managed login, the user's config ignored).
2. **Record.** Each answer goes to `qae/ACn-reference-check.json`: the step, the reference, the claim, the
   digests of both images, the answer, or the reason there is none (a usage limit, a timeout). Records left from
   before are removed first.
3. **Judge.** The verify job's [`acceptance-verdict`](../../gates/acceptance_verdict.py) gate counts such a FAIL
   as a PASS anchored at that step only when the record matches the FAIL's claim, its step and reference, and both
   images as the evidence holds them, the reference is one the criterion names (so the workflow's digest vouches
   for it), the model did not see the difference, and `other` is false. It prints that it did not count the FAIL, and why. A record
   that confirms the difference, one that did not run, a missing one, or one about another claim or other images
   leaves the FAIL standing. A FAIL on anything but a comparison is never asked about.

The explorer is told to walk the rest of the criterion after a difference and to cite the comparison step only
when no other step failed, so a refuted FAIL leaves a criterion whose other steps held.

## In a QAE workflow

`harnesses/qae/explore-codex.yml` has the step after the explorer, on the runner that holds its Codex login:

```yaml
      - name: Look again at each reference FAIL
        if: ${{ !cancelled() }}
        uses: Greenbauer/vibe-verifier/actions/reference-check@<sha>
        with:
          model: gpt-6.1-sol
          reasoning-effort: high
```

A check that did not run is a warning, not a failed step: its FAIL stands. The Claude lane (`explore.yml`) has no
Codex login and no step, so there every comparison FAIL counts as before.

## Tests

`tests/test_reference_check.py` runs the grader with a fake Codex CLI and the gate on the records it writes. Its
`Model` case asks the real model about made-up pages in `tests/fixtures/reference-check/`, in the shape of that run:
`close-x`, the invented close X, which it must reject; `no-modal`, the same reference against a screen with the modal
closed, which it must confirm; and `close-x-timeout`, the invented X plus a save that could not be checked, which it
must mark `other`. It needs a Codex login: `REFERENCE_CHECK_CODEX_HOME=~/.codex python3 -m unittest
test_reference_check` from `tests/`.
