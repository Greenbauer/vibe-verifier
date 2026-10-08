# Usage artifact contract

## Requirements ledger

The source for U1 through U6 is the approved dashboard usage scope dated 2026-09-30.

| ID | Checkable requirement | Proof before integration |
| --- | --- | --- |
| U1 | One versioned record identifies the provider, role, optional caller-owned account alias, repository owner/name, full head revision, run, attempt, job, observation time, availability, and validated token integers. Missing usage is unavailable or partial, never zero. | Schema fixtures and validation tests. |
| U2 | The Codex runner consumes JSONL without logging or retaining it, adds only `turn.completed.usage`, keeps `final.md`, forwards signals to its child, and returns the child's exit status after writing any partial usage. | Fake Codex subprocess tests for success, failure, malformed events, large lines, content privacy, and signals. |
| U3 | Claude collection reads only the final top-level result usage from the bounded execution file. It does not add assistant-message usage or `modelUsage`. Skipped, missing, failed, and malformed sources remain explicit without changing review receipt rules. | Claude execution-file fixtures and workflow ordering tests. |
| U4 | Each applicable source job uploads only `usage.json` as `vv-usage-<role>-<run_attempt>` (a QAE explorer past the first of a split run appends `-<explorer>`) for 7 days through the repository's established pinned upload action. No new permission or secret is added. | Harness and action structure tests. |
| U5 | Tests cover normal and multi-event use, partial failure, real zero use, absent use, duplicate-source avoidance, cache fields, invalid numbers and types, arbitrary secret-shaped content, path and size bounds, and exit preservation. Files remain at most 500 lines. | Focused unit tests, existing harness tests, workflow lint, and the file-length gate. |
| U6 | Public docs define the schema, privacy boundary, provider counting rules, applicability, retention and deletion, the 7-day dashboard window, lack of backfill, and dashboard ingestion. | This document plus the review and QAE harness docs. |

Reviewer usage collection and upload follow the review receipt and run under `always()`. Capture failures still fail the review job but cannot suppress a valid completed-review receipt.

The deterministic verifier jobs do not run a model, so token usage is not applicable to them and no usage artifact is created for them. Existing runs have no structured usage and cannot be backfilled.

## Version 1 schema

Every artifact contains one file named `usage.json`:

```json
{
  "schema_version": 1,
  "status": "complete",
  "status_reason": null,
  "provider": "openai",
  "role": "qae-explorer",
  "account_alias": null,
  "owner": "example",
  "repository": "example/site",
  "head_sha": "0123456789abcdef0123456789abcdef01234567",
  "run_id": 123456789,
  "run_attempt": 1,
  "job_key": "explore",
  "observed_at": "2026-09-30T18:00:00Z",
  "usage": {
    "input_tokens": 1200,
    "cached_input_tokens": 800,
    "cache_creation_input_tokens": null,
    "output_tokens": 90,
    "reasoning_output_tokens": 20
  }
}
```

`status` is `complete`, `partial`, or `unavailable`. `usage` is `null` when no valid final usage exists. A legitimate all-zero usage object remains `complete`, which distinguishes it from missing data. `status_reason` is a small structural enum such as `not_run`, `no_artifact`, `no_final_usage`, `invalid_usage`, or `provider_failed`; it never contains provider output.

`account_alias` is either `null` or an explicit logical alias supplied by the workflow owner. `null` means unmapped. Importers must not assume that two unmapped records, or two records from the same provider, use the same subscription. The field must not contain an email address, billing identity, credential, or access token.

The only model-process values admitted to the artifact are non-negative integer token counts. Metadata comes from the GitHub run. Prompts, messages, results, tool output, stderr, execution files, and raw event streams are never copied into the artifact or collector logs.

## Provider mappings

For Codex, the source is each numeric `turn.completed.usage` object from `codex exec --json`. `input_tokens` already includes cached input, so consumers must not add `cached_input_tokens` to it. `cache_creation_input_tokens` is `null` because that source does not report it.

For Claude, the source is only `usage` on the final top-level `type: "result"` entry in the pinned action's execution file. The action writes its SDK message array to that file. The review harness pins v1.0.171, which locks `@anthropic-ai/claude-agent-sdk` 0.3.207; the QAE explorer pins v1.0.105, which locks 0.2.119. Its result usage fields are mapped as follows:

| Claude result field | Version 1 field |
| --- | --- |
| `input_tokens` | `input_tokens` |
| `cache_read_input_tokens` | `cached_input_tokens` |
| `cache_creation_input_tokens` | `cache_creation_input_tokens` |
| `output_tokens` | `output_tokens` |
| not reported | `reasoning_output_tokens: null` |

Claude's input, cache-read, and cache-creation fields are distinct. Consumers may add those three only when they need a provider-specific total input count. The collector does not sum assistant messages, the final result, and `modelUsage`, because those are overlapping representations.

Primary source references: for the review harness, the [pinned action output and dependency lock](https://github.com/anthropics/claude-code-action/tree/e90deca47693f9457b72f2b53c17d7c445a87342/base-action) and the [pinned execution-file writer](https://github.com/anthropics/claude-code-action/blob/e90deca47693f9457b72f2b53c17d7c445a87342/base-action/src/execution-file.ts); for the QAE explorer, the [pinned action output and dependency lock](https://github.com/anthropics/claude-code-action/tree/e58dfa55559035499a4982426bb73605e8b5ad8e/base-action) and the [pinned execution-file writer](https://github.com/anthropics/claude-code-action/blob/e58dfa55559035499a4982426bb73605e8b5ad8e/base-action/src/run-claude-sdk.ts).

## Storage and lifecycle

The source workflow uploads a dedicated artifact named `vv-usage-swe-reviewer-<run_attempt>` or `vv-usage-qae-explorer-<run_attempt>` with `retention-days: 7`. When a pull request's criteria are split across several QAE explorers, each past the first uploads its own as `vv-usage-qae-explorer-<run_attempt>-<explorer>`. The dashboard's importer matches the name without that suffix only, so it does not read those artifacts yet: a split run's tokens past the first explorer are not counted there until it does. The artifact contains only `usage.json`. GitHub deletes it when retention expires; repository operators may delete a run or its artifacts earlier through GitHub. The dashboard consumer must verify the source run ID and attempt against GitHub, and it reads only the newest 7 days.

The SWE reviewer and QAE explorer are applicable model roles. Review verification and QAE verification are deterministic and have no token record. Counts describe observed tokens, not a subscription quota, remaining allowance, price, or billing total.

## Dashboard ingestion

`dashboard/usage_artifacts.py` imports these artifacts, checks the configured repository owner,
source workflow, run ID, attempt and head against GitHub, and keeps unmapped accounts separate.
`dashboard/live_service.py` combines their numeric samples with independently observed quota and
capacity. The browser totals measured tokens while keeping subscription percentages separate.
The collector rejects archives containing anything other than a bounded `usage.json` file.

Consumer workflows need the updated action pin and, for the Claude harnesses, the capture/upload
steps from the template. Existing runs cannot acquire missing history. A live capture is verified
only after a real source run produces an inspected numeric artifact.
