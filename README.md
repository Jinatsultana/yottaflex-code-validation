# Versioned Code Validation Pipeline

A small, locally runnable data-engineering solution for validating incoming codes against **versioned reference datasets**. It covers schema checks, effective dates, deprecated and unknown codes, quarantine, deterministic run IDs, idempotent reruns, operational metrics, reconciliation, tests, and scalable synthetic data generation.

## Why this design

- **Python standard library + SQLite:** no service or credentials required; SQLite persists run history and outputs. This is a deliberately small vertical slice rather than an enterprise platform.
- **CSV contracts:** inputs and reference data are easy to inspect and reproduce.
- **Explicit accepted/rejected paths:** every rejected source row keeps its source filename, line number, raw record, and reason code.
- **Reference version is a run parameter:** choose `v1` or `v2`; reference refreshes are represented as new version rows, not silent overwrites.
- **Deterministic run ID:** SHA-256 of input bytes plus reference version. Re-running the same file/version replaces that run's outputs rather than duplicating them.

## Requirements

- Python 3.10+
- `pytest` only for running tests (the pipeline itself has no third-party runtime dependencies)

## Quick start

From the repository root:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e '.[test]'
python -m code_validator.cli run
```

If using the source tree without installing the package, run `PYTHONPATH=src python -m code_validator.cli run` (PowerShell: `$env:PYTHONPATH='src'; python -m code_validator.cli run`).

Outputs are written to `outputs/`:

- `accepted_records.csv` — accepted and enriched records
- `rejected_records.csv` — quarantined records and reasons
- `run_report.json` — run metrics and reject counts by reason
- `validation.db` — persisted run history, accepted records, and rejected records

The command prints the `run_id`. Reconcile the counts using:

```bash
python -m code_validator.cli reconcile --run-id YOUR_RUN_ID
```

To select a refreshed reference dataset:

```bash
python -m code_validator.cli run --reference-version v2
```

## Generate a larger deterministic dataset

```bash
python scripts/generate_data.py --rows 100000 --seed 42 --output data/input/generated_codes.csv
python -m code_validator.cli run --input data/input/generated_codes.csv
```

The generator uses a fixed seed and deliberately includes invalid dates, missing entity IDs, unknown/deprecated/future/retired codes, and duplicate event IDs. Generated input is synthetic.

## Data model and dictionary

### Incoming input: `incoming_codes.csv`

| Column | Type | Required | Meaning / rule |
|---|---|---:|---|
| `event_id` | string | yes | Stable event key; unique within a batch. A repeated key is quarantined. |
| `entity_id` | string | yes | Source business entity identifier. |
| `code` | string | yes | Code to validate against the chosen reference version. |
| `event_date` | ISO date | yes | Business date used for effective-date validation (`YYYY-MM-DD`). |

### Reference: `reference_codes.csv`

| Column | Type | Required | Meaning / rule |
|---|---|---:|---|
| `reference_version` | string | yes | Immutable logical snapshot/version, e.g. `v1`, `v2`. |
| `code` | string | yes | Code key within a version and non-overlapping validity windows. |
| `description` | string | yes | Human-readable label used to enrich accepted rows. |
| `valid_from` | ISO date | yes | Inclusive start date. |
| `valid_to` | ISO date / blank | no | Inclusive end date; blank means no known end. |
| `deprecated` | boolean-like string | yes | `true/false` (also accepts `1/0`, `yes/no`); deprecated codes are rejected. |

### Persisted entities

- `run_history`: one row per deterministic run ID, timestamps, status, row counts, duration, and failure reason.
- `accepted_records`: one row per `(run_id, event_id)`, enriched with reference description/version and source location.
- `rejected_records`: quarantined source rows with a machine-readable reason code and raw-record JSON.

Grain is one source event per event ID per run. Foreign-key relationships are not asserted between incoming entity IDs and a separate entity master because the brief does not provide an entity reference dataset. `entity_id` is therefore required but not independently referentially validated.

## Validation behavior

- Missing input columns or malformed reference schema: run fails loudly and emits a `pipeline_failure` quarantine artifact/report.
- Missing required row values: `missing_required_field`.
- Extra CSV values beyond the header: `malformed_record`.
- Invalid date format: `invalid_event_date`.
- Repeated `event_id` in a batch: first occurrence is processed; subsequent occurrences get `duplicate_event_id`.
- Code absent from selected version: `unknown_code`.
- Event date outside the code's inclusive effective window: `outside_effective_dates`.
- Code marked deprecated: `deprecated_code`.
- Reference effective windows that overlap for the same code/version are rejected during reference loading to avoid ambiguous matches.
- Unknown columns are tolerated to permit additive schema evolution; required-column removal is not.

## Architecture / flow

```text
CSV input -> schema validation -> row-level checks -> versioned reference/effective-date lookup
         -> accepted + enriched records -> SQLite + accepted CSV
         -> quarantine with source trace + SQLite + rejected CSV
         -> run metrics/report -> reconciliation assertion
```

## Testing and correctness evidence

```bash
pytest -q — 11 tests passing, including invalid reference-schema handling.
```

Tests cover a happy path, empty input, missing required values, duplicate event IDs, malformed extra fields, unexpected schema change, reference-version refresh, idempotent rerun, effective-date boundary behavior, unknown codes, deprecated codes, and missing input files. Reconciliation verifies `input_rows = accepted_rows + rejected_rows` and confirms persisted table counts agree with run metrics.

For manual inspection, review `outputs/run_report.json` and `outputs/rejected_records.csv` after running the sample. The sample includes intentionally invalid records; a non-zero reject count is expected.

## Performance measurement and optimization

Each run records elapsed wall-clock time in `duration_ms`. To measure a larger run, generate 100,000 rows and compare duration and rows/second across runs. Current reference lookup uses an in-memory dictionary indexed by code, so lookup avoids scanning the entire reference table for every input row. A next optimization for substantially larger data is streaming input rows in chunks rather than retaining all accepted/rejected rows in memory; SQLite batch inserts are also a natural extension.

## Idempotency, retries, backfills, and reference refreshes

- The run ID is deterministic for the exact input file bytes and selected reference version.
- On rerun, rows and run metrics for that run ID are replaced transactionally in SQLite; repeated execution does not accumulate duplicates for that run.
- A changed input file or a different reference version produces a different run ID and preserves separate run history.
- To backfill, rerun the historical input with the intended reference version and retain the resulting run ID/report. The current implementation uses the source filename as trace metadata; the input bytes determine the run ID.
- A schema/reference failure is marked `failed`, logged, and returns a non-zero CLI exit status. Fix the file/configuration and retry.

## Assumptions and limitations

1. `event_date` is a date, not a timestamp; effective boundaries are inclusive.
2. Deprecated codes are rejected rather than automatically mapped to replacements because no replacement mapping was specified.
3. `event_id` is the batch-level event key; duplicates are handled within the batch. Cross-run duplicates are isolated by run ID to preserve replay history.
4. The implementation uses local CSV and SQLite only; it does not include a scheduler, API ingestion, distributed processing, alerting, or cloud deployment.
5. A pipeline-level failure records a failure artifact but cannot guarantee per-row quarantine when the source schema itself cannot be parsed.
6. Reference versions are supplied in one CSV file for reproducibility; in production they could be immutable snapshots stored separately with checksums and approval metadata.

## AI-use disclosure

AI tools were used for implementation assistance, test planning, and documentation drafting. The candidate should review and understand the implementation, run the tests and sample pipeline, inspect generated outputs, and update `prompt_history/` with the actual prompts used across all AI tools before submitting. Do not claim checks were run unless you ran them yourself.

## Repository hygiene

Do not commit credentials, tokens, personal information, virtual environments, or local database files. See `.gitignore`. Keep the public repository limited to synthetic sample data and source artifacts.
