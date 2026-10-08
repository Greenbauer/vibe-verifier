# Dashboard agent identities

The roster is the identities an owner configures, retaining numbers where present. A mapped
QAE is shown once per lane instance. Workflow stages such as review, exploration, and
deterministic verification are not separate agents. An owner who lists a reviewer still sees SWE.

## Requirements and verification

- Only an explicitly configured SWE/QAE roster creates agent tiles; preserve names and IDs.
  Verify with config tests and a populated browser view.
- Attribute runtime state, recent outcomes (at most five in two hours), failures, and usage
  through the same identity. Never infer agent health from runner availability.
  Verify stale, missing, foreign, and populated telemetry cases.
- An explicit CI workflow mapping may show one SWE, or one QAE split into lane instances
  (QAE 1, QAE 2, and so on), from that workflow's jobs. It does not claim a numbered persistent
  agent. A run whose runner name is not a lane instance stays on the unnumbered QAE, shown only
  when it has activity. Deterministic checks remain in PR progress.
- Keep the combined usage chart and individual agent charts, distinct SWE/QAE colors,
  owner isolation, and the existing runner/PR views. Verify a populated browser and tests.
- Keep status cards content-sized and packed against the left edge, with no outer strip
  padding. Preserve the compact horizontal row on small screens, scrolling when needed.
  Verify populated desktop and mobile layouts, including the status-to-usage shortcut.

The roster is immutable until restart and lives in the private deployment configuration.
Runtime telemetry is atomically replaced by its existing collector; readers cannot edit it.
Only seven days of outcomes and usage are displayed. Removing an identity from configuration
removes its display and attribution; this does not delete or operate the source agent.

## Runtime collector contract

Add `agents` beside `capacity` and `usage` in the owner-scoped telemetry file:

```json
{
  "sampled_at": "2026-09-30T15:00:00Z",
  "rows": [{
    "id": "configured-agent-id",
    "state": "paused",
    "runs": [{"id": "run-id", "completed_at": "2026-09-30T14:50:00Z",
              "category": "failed", "elapsed_seconds": 45}]
  }]
}
```

This is the `agents` section, not a complete telemetry file. IDs must match the private
configuration. States are working, idle, paused, down, or unknown. Send at most five latest
completed runs from seven days, with success, failed, or cancelled categories. Unknown fields,
foreign IDs, raw logs, and oversized histories are rejected. Agent state older than five
minutes stays as last observed and is marked stale; observed outcomes retain their timestamps. Usage samples use this same configured
agent ID in `bot`; unrelated CI artifacts never become a numbered agent's tokens. Runtime-only
rosters do not request CI usage artifacts.
