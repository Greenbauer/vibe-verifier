// The suite side of the seats contract. Run by tests/test_browser_seats.py, which passes the
// action's own `extra-seats` output as EXTRA_SEATS so both halves are checked against one value;
// alone (`node --test`), it builds the same three extra seats itself.
import assert from "node:assert/strict";
import test from "node:test";

import { parseSeats, selectSeat, workersFor } from "./select.mjs";

const login = (role, seat) => ({
  email: `${role}+s${seat}@preview.example.test`,
  password: `pw-${role}-${seat}`
});
const seat = (number) => ({
  traveler: login("traveler", number),
  partner: login("partner", number)
});
const extra = process.env.EXTRA_SEATS || JSON.stringify([seat(1), seat(2), seat(3)]);
const roles = {
  traveler: ["E2E_TRAVELER_EMAIL", "E2E_TRAVELER_PASSWORD"],
  partner: ["E2E_PARTNER_EMAIL", "E2E_PARTNER_PASSWORD"]
};
// Seat 0 as the suite's job already passes it, plus the worker's index when it has one.
const environment = (index) => ({
  E2E_TRAVELER_EMAIL: seat(0).traveler.email,
  E2E_TRAVELER_PASSWORD: seat(0).traveler.password,
  E2E_PARTNER_EMAIL: seat(0).partner.email,
  E2E_PARTNER_PASSWORD: seat(0).partner.password,
  ...(index === undefined ? {} : { TEST_PARALLEL_INDEX: String(index) })
});

test("the action's output gives worker k seat k", () => {
  assert.equal(workersFor(extra), 4);
  assert.deepEqual(parseSeats(extra), [seat(1), seat(2), seat(3)]);
  for (const index of [0, 1, 2, 3]) {
    const env = environment(index);
    assert.equal(selectSeat(env, extra, roles), index);
    assert.equal(env.E2E_TRAVELER_EMAIL, seat(index).traveler.email);
    assert.equal(env.E2E_TRAVELER_PASSWORD, seat(index).traveler.password);
    assert.equal(env.E2E_PARTNER_EMAIL, seat(index).partner.email);
    assert.equal(env.E2E_PARTNER_PASSWORD, seat(index).partner.password);
  }
});

test("a run without extra seats keeps every worker on seat 0", () => {
  assert.equal(workersFor(""), 1);
  for (const index of [undefined, 0, 3]) {
    const env = environment(index);
    assert.equal(selectSeat(env, "", roles), 0);
    assert.equal(env.E2E_TRAVELER_EMAIL, seat(0).traveler.email);
  }
});

test("a worker without a seat refuses to share one", () => {
  assert.throws(
    () => selectSeat(environment(2), JSON.stringify([seat(1)]), roles),
    /worker 2 has no seat/
  );
  assert.throws(
    () => selectSeat(environment(1), JSON.stringify([{ traveler: { email: "x@y.test" } }]), roles),
    /seat 1 needs an email and password/
  );
  assert.throws(
    () => selectSeat(environment(1), JSON.stringify([{ traveler: login("traveler", 1) }]), roles),
    /seat 1 has no partner/
  );
});
