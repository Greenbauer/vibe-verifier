# Browser seats

A browser suite that signs in to a per-PR preview usually runs on one worker, because every test
shares one seeded account: two workers on one account edit the same records, friend lists and
journeys, and fail each other. Seats remove that limit. The preview seeds N seats, each a full copy
of the test accounts and the data they own, and the suite runs one worker per seat inside its one
job. [`seats.py`](seats.py) checks the seats and hands them to the suite;
[`select.mjs`](select.mjs) gives worker k seat k.

Measured on the first consumer (a 419-test Playwright suite, 166 files, 2026-10-13): one worker took
37 minutes of test time; four seats and four workers took 10.7 minutes, and the job 11m44s instead
of about 40.

## Measure first

Count where the suite's time goes before seeding anything. From a run's JSON report, sum each
spec file's test durations and mark which files sign in. On the first consumer 133 of 166 files
(34.6 of 37 minutes) signed in, so moving the files that do not into parallel shards would have
saved about 3 minutes. If most of your time is in files that never sign in, run those in parallel
and skip seats.

## The contract

**Seats.** The preview seeds seats 0 to N-1. A seat is one login per role (for example a traveler
and a partner), and everything those logins own at seed time is the seat's own copy, with its own
ids. Seat 0 is the reference seat: it keeps the logins and ids the preview had before seats, and
serves the long-lived main preview and people testing by hand.

**One seat per check that runs at the same time.** Every check that signs in to a pull request's
preview while another one does (each suite worker, a geometry audit, each QAE explorer) gets seats
no other running check uses. Two checks on one seat must run one after the other, and that wait is
what seats remove: on the first consumer the QAE explorers shared seat 0 with the suite and a
geometry audit, and waited a median 26 minutes for both to finish, about half of the 50 minutes from
a push to its last check (11 pull request heads, 2026-10-14). So a check waits only for what it
reads (the preview being deployed), never for another check to free a login. With a seat each
(geometry audit on seat 0, the suite's four workers on seats 1 to 4, QAE explorer k on seat 4 + k),
all three started within a minute of the deploy and the last check landed at 22 minutes; nine
browsers on one preview caused no failures, and each extra seat added about 4 seconds to seeding.
A preview no pull request reads from (a long-lived main preview with no audit or QAE) gives the
suite every seat, seat 0 included.

The suite's seats file then lists the suite's own seats only, in worker order: its first entry is
the login the suite already reads, whichever seat number the preview gave it.

**Ids.** A spec never hard-codes a seeded id. It reads its worker's copy from one helper, which
applies the same rule the seed did. The first consumer replaces the fourth group of every seeded
UUID with `8000 + k` in hex, so seat 1's journey `20000000-0000-4000-8000-000000000001` becomes
`20000000-0000-4000-8001-000000000001`. Pick one rule, write it once on each side, and test both
against the same examples.

**Names that are searched.** Anything a spec searches by substring (a username in a people search)
must not contain another seat's. `preview-partner` and `preview-partner-s1` both match a search for
`preview-partner`; `preview-partner` and `seat-1-partner` do not. Display names can stay the same
on every seat, so a spec asserts the same text whichever seat runs it.

**Hand-off.** The suite's job writes its seats' logins to a file, the login it already reads first, as a JSON list of
role -> `{"email", "password"}`, and runs this action on it:

```yaml
      - name: Check the preview's seats
        id: seats
        uses: Greenbauer/vibe-verifier/actions/browser-seats@0000000000000000000000000000000000000000 # CONSUMER: pin the commit you subscribe to
        with:
          seats-file: seats.json          # CONSUMER: written from your secret store with umask 077
          email-domain: preview.example.test   # CONSUMER: your preview's test domain, or omit
      - name: Run the browser suite
        env:
          E2E_EXTRA_SEATS: ${{ steps.seats.outputs.extra-seats }}  # CONSUMER: any name your suite reads
        run: npx playwright test
```

The action fails, before any browser starts, when the list is empty, over `max-seats` (8), a seat
names other roles than seat 0, a login has no email or password, a login is off `email-domain`, or
two seats share a login. It masks every password and the `extra-seats` value. Its outputs are
`workers`, the number of seats, and `extra-seats`, every entry after the first as compact JSON (`[]`
for one seat). Pass the first entry to the suite the way you already do.

**Workers.** The suite runs `1 + extra seats` workers in CI and keeps a spec file on one worker
(Playwright's `fullyParallel: false`), and worker k signs in as seat k:

```js
import { selectSeat, workersFor } from "./seats.mjs"; // CONSUMER: or your own copy of the contract

// playwright.config: one worker per seat in CI.
workers: process.env.CI ? workersFor(process.env.E2E_EXTRA_SEATS) : undefined,

// Your fixtures module, which every spec imports, before any spec reads a login:
const seat = selectSeat(process.env, process.env.E2E_EXTRA_SEATS, {
  traveler: ["E2E_TRAVELER_EMAIL", "E2E_TRAVELER_PASSWORD"],
  partner: ["E2E_PARTNER_EMAIL", "E2E_PARTNER_PASSWORD"]
});
```

`selectSeat` re-points the variables seat 0 already uses, so specs keep reading the same names.
With no extra seats (a local run) every worker keeps seat 0, as before. Your report redactor must
redact every seat's password, not only seat 0's.

## One job, N workers, not N jobs

Run the workers inside the suite's one job rather than sharding it across runner jobs
(`--shard i/N`). The suite needs the seats either way. One job keeps one report, so anything that
reads it (proof scoring, regression attribution, a status) works unchanged, and it holds one runner
slot instead of N on a lane that also serves other suites, QA and deploys. Shard across jobs only
when one runner cannot hold N browsers: the first consumer ran four Chromium workers in a 4 GB
container.

## What broke on the way (check yours)

- **A database statement per seat.** The first consumer seeds through the RDS Data API, which takes
  at most 65,536 characters of SQL. One seat's rows were about 28,000, so four seats in one statement
  failed. Seed each seat in its own statement, and let only seat 0's delete rows no seat owns.
- **Configuration that names seat 0.** A spec that reads routes from another tool's configuration
  (a geometry audit's routes) walks seat 0's records. Map them to the worker's seat.
- **Late renders surface under load.** Four browsers on one runner are slower than one. A spec that
  acts before a page finished loading, and passed by luck on one worker, can now fail. Fix the spec
  with a readiness wait. Never drop the workers to hide it.
- **ESM cannot see `export *`.** A spec in an ESM package could not import a name a transpiled
  CommonJS fixtures module re-exported with `export *`, and the suite loaded nothing. Import such a
  name from its own module, and check with `playwright test --list` and the seats set.
- **Size gates.** Seat code added to large files tripped the catalog's `max-file-lines` and
  `cognitive-complexity` ratchets. New modules for the seat code kept every touched file within them.

## Limits

- The seats are only as separate as the seed makes them. Anything global (a public catalog, a shared
  sample whose source is seat 0's journey) is still shared, and a spec that edits it still collides.
- More seats cost more seeded logins and secrets per preview, and more browsers in one container.
  Size N to the runner, not to the suite.
