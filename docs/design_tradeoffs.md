# Design and Trade-off Note

## Decisions

1. **Local-first execution:** Python and SQLite avoid cloud credentials and make clean-environment review straightforward.
2. **Version explicitness:** the caller selects a reference version; versions coexist in the reference file. This makes a refresh reproducible and avoids silently applying a changed reference snapshot to historical results.
3. **Effective-date semantics:** `valid_from` and `valid_to` are inclusive. Ambiguous overlapping ranges for the same code/version fail the run.
4. **Reject, don't discard:** rejected rows retain source file, source line, raw row, and reason code. A pipeline-level failure is separately recorded.
5. **Deterministic replay:** input content plus reference version determines the run ID; the same run is replaced rather than duplicated.
6. **Explicit deprecation policy:** deprecated codes are rejected. Mapping to replacement codes is not implemented because the problem statement provides no replacement mapping.

## Trade-offs / limitations

- Keeping accepted/rejected rows in memory makes the starter solution easy to read but is not ideal for multi-million-row files. Stream/chunk processing and bulk inserts are the next step.
- SQLite is suitable for a local challenge but not concurrent distributed writers or large multi-user workloads.
- Event uniqueness is checked within a batch; separate runs are retained as distinct historical executions.
- Additive unknown input columns are tolerated, but required-column removal fails. A stricter contract could reject all unknown columns if the source requires exact schemas.
- Failure artifact is batch-level when a file cannot be parsed or its schema is broken; row-level rejects are produced when rows can be read.

## Validation evidence to collect before submission

- `pytest -q` output
- sample `run_report.json`
- reconciliation command output
- a 100,000-row run duration and approximate throughput
- manual inspection of representative accepted and rejected records
