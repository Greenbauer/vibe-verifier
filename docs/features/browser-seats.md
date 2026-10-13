# Browser seats

Checks the seats a per-PR preview seeds (seat 0 the reference seat, seats 1 and up for the suite) and hands them to a browser suite that runs one worker per seat in its one job, so parallel workers never share an account or the data it owns.

## Surfaces

- source: `actions/browser-seats/seats.py`
- source: `actions/browser-seats/action.yml`
- source: `actions/browser-seats/select.mjs`

## Reach

From a workflow, `uses: Greenbauer/vibe-verifier/actions/browser-seats@<sha>` with `seats-file:` in the job that runs the browser suite, and the suite's fixtures call `selectSeat` from `select.mjs` (or the consumer's own copy of the contract) ([actions/browser-seats/README.md](../../actions/browser-seats/README.md)). From a clone, `python3 actions/browser-seats/seats.py --file seats.json` checks a seat list.

## Verify

- `tests/test_browser_seats.py::test_four_seats_make_four_workers_and_hand_on_seats_1_to_3_masked`
- `tests/test_browser_seats.py::test_one_seat_runs_one_worker_and_masks_no_empty_list`
- `tests/test_browser_seats.py::test_a_list_a_suite_must_not_run_on_fails_before_any_browser`
- `tests/test_browser_seats.py::test_from_a_clone_the_engine_checks_a_seat_list`
- `tests/test_browser_seats.py::test_the_suite_side_gives_worker_k_seat_k_from_the_action_output`

## Gotchas

- Measure first: seats pay only when most of the suite's time is in tests that sign in.
- Seat 0 keeps the preview's original logins and ids; everything that is not the suite uses only seat 0.
- A searched name (a username) must not contain another seat's, or a substring search finds the wrong seat.
- The seats are only as separate as the seed makes them: anything global is still shared.
