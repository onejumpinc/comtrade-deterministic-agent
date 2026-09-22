# Deterministic ComtradeBench Agent

A model-free AgentBeats participant for the seven public tasks in
[ComtradeBench](https://github.com/yonghongzhang-io/green-comtrade-bench-v2).
It reads the green-configured mock API, paginates its live responses, retries
real HTTP 429/500 failures, removes marked totals, deduplicates records, and
writes the benchmark's three required artifacts.

## Integrity properties

- The participant never calls the mock service's `/configure` endpoint. The
  green agent remains authoritative for task configuration.
- The runtime contains no task table, fixture data, expected row counts,
  reporter/partner values, record IDs, or answer cache.
- `total_rows`, page size, query identity, and records all come from live HTTP
  responses.
- HTTP retry counts and 429/500 log entries are recorded only when those
  responses occur.
- A non-canonical live page order triggers one full live snapshot, so page
  drift is handled without inspecting the task name or an expected record set.
- Output rows are schema-checked, totals-filtered, deduplicated, and sorted
  before being written atomically.
- Every completion log records truthful `warnings` and `errors` counts,
  including zero values, as part of the audit trail expected by the judge.

## Verified score

The unmodified upstream `src.judge.score_output` reports `100.0` for every
task and an empty error list. The public
[release run](https://github.com/onejumpinc/comtrade-deterministic-agent/actions/runs/35687874930)
produced `1400/1400` across two fresh Linux/AMD64 full-stack suites, with
identical `data.jsonl` SHA-256 values for all seven tasks. It published the
exact tested image at
`ghcr.io/onejumpinc/comtrade-deterministic-agent@sha256:e22724362f756ad89115f406ff376ea6263375e76eebe574bedfdc0d9454239c`.

| Task | Requests | Score |
| --- | ---: | ---: |
| `T1_single_page` | 1 | 100.0 |
| `T2_multi_page` | 5 | 100.0 |
| `T3_duplicates` | 3 | 100.0 |
| `T4_rate_limit_429` | 4, including the real 429 | 100.0 |
| `T5_server_error_500` | 4, including the real 500 | 100.0 |
| `T6_page_drift` | 2 | 100.0 |
| `T7_totals_trap` | 3 | 100.0 |

The release workflow repeats the complete seven-task assessment twice through
the pinned green agent, mock service, A2A client, and exact image that it later
publishes. Publication is gated on `1400/1400`, empty judge error lists, exact
request counts, live fault evidence, deterministic hashes, and immutable image
provenance.

## Development

```bash
uv sync --frozen --extra test
uv run pytest -q
```

The container listens on port `9009` and must retain the participant role name
`purple-comtrade-baseline-v2`, because that is the role required by the pinned
green agent.

This repository is a fork of the benchmark author's reference participant and
retains its history. The runtime implementation has been replaced to avoid the
reference participant's embedded task table and mock reconfiguration.
