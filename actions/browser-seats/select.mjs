// The browser-suite side of the seats contract (README.md): worker k signs in as seat k. A suite
// imports this (or mirrors it; the contract is the input and output, not this file) from its test
// configuration and its fixtures, which every worker loads before any test reads a login.
// No dependencies, so a TypeScript or JavaScript suite can use it as it is.

// The extra seats (seats 1 and up) from the action's `extra-seats` output, or [] when none were
// given. Throws on a list that is not the contract's shape.
export function parseSeats(raw) {
  if (!raw) return [];
  const seats = JSON.parse(raw);
  if (!Array.isArray(seats))
    throw new Error("the extra seats must be a JSON list of seats");
  for (const [index, seat] of seats.entries()) {
    const logins = seat && typeof seat === "object" ? Object.values(seat) : [];
    if (
      logins.length === 0 ||
      logins.some((login) => !login?.email || !login?.password)
    )
      throw new Error(`seat ${index + 1} needs an email and password for every role`);
  }
  return seats;
}

// How many workers the suite runs: seat 0 plus every extra seat.
export function workersFor(raw) {
  return 1 + parseSeats(raw).length;
}

// Points this worker's login variables at its seat and returns the seat number. `roles` maps each
// role to the [email, password] variable names the suite already reads for seat 0, so specs keep
// reading the same names. Playwright gives a worker a stable TEST_PARALLEL_INDEX (a replacement
// worker reuses it), so two workers never hold one seat. With no extra seats at all (a local run),
// every worker keeps seat 0, as before seats existed.
export function selectSeat(environment, raw, roles) {
  const seat = Number(environment.TEST_PARALLEL_INDEX ?? 0);
  if (!Number.isSafeInteger(seat) || seat < 0)
    throw new Error("TEST_PARALLEL_INDEX must be a non-negative integer");
  if (seat === 0 || !raw) return 0;
  const login = parseSeats(raw)[seat - 1];
  if (!login)
    throw new Error(
      `worker ${seat} has no seat: run at most as many workers as there are seats`
    );
  for (const [role, [emailVariable, passwordVariable]] of Object.entries(roles)) {
    if (!login[role]) throw new Error(`seat ${seat} has no ${role}`);
    environment[emailVariable] = login[role].email;
    environment[passwordVariable] = login[role].password;
  }
  return seat;
}
