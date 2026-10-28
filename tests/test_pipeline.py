import csv
import json
from pathlib import Path

from code_validator.pipeline import reconcile, run_pipeline

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data/reference/reference_codes.csv"

def write_csv(path, headers, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f); writer.writerow(headers); writer.writerows(rows)

def run(tmp_path, rows, headers=("event_id", "entity_id", "code", "event_date"), version="v1"):
    source = tmp_path / "incoming.csv"
    write_csv(source, headers, rows)
    return run_pipeline(source, REF, tmp_path / "db.sqlite", tmp_path / "out", version), source, tmp_path / "db.sqlite"

def test_sample_expected_results(tmp_path):
    metrics, _, db = run(tmp_path, [
        ["1", "E1", "A100", "2026-10-01"], ["2", "E2", "Z999", "2026-10-01"],
        ["3", "E3", "C300", "2026-10-01"], ["4", "E4", "D400", "2026-10-01"],
        ["5", "E5", "A100", "bad-date"], ["6", "E6", "A100", "2026-10-02"]])
    assert metrics["status"] == "success"
    assert (metrics["input_rows"], metrics["accepted_rows"], metrics["rejected_rows"]) == (6, 2, 4)
    assert reconcile(db, metrics["run_id"])["reconciled"] is True
    rejects = (tmp_path / "out/rejected_records.csv").read_text()
    for reason in ("unknown_code", "outside_effective_dates", "deprecated_code", "invalid_event_date"):
        assert reason in rejects

def test_empty_input(tmp_path):
    metrics, _, db = run(tmp_path, [])
    assert metrics["status"] == "success"
    assert metrics["input_rows"] == metrics["accepted_rows"] == metrics["rejected_rows"] == 0
    assert reconcile(db, metrics["run_id"])["reconciled"] is True

def test_missing_required_field_is_quarantined(tmp_path):
    metrics, _, _ = run(tmp_path, [["1", "", "A100", "2026-10-01"]])
    assert metrics["rejected_rows"] == 1
    assert "missing_required_field" in (tmp_path / "out/rejected_records.csv").read_text()

def test_duplicate_event_id_is_rejected(tmp_path):
    metrics, _, _ = run(tmp_path, [["1", "E1", "A100", "2026-10-01"], ["1", "E2", "A100", "2026-10-02"]])
    assert metrics["accepted_rows"] == 1 and metrics["rejected_rows"] == 1
    assert "duplicate_event_id" in (tmp_path / "out/rejected_records.csv").read_text()

def test_malformed_extra_csv_value(tmp_path):
    metrics, _, _ = run(tmp_path, [["1", "E1", "A100", "2026-10-01", "unexpected"]])
    assert metrics["rejected_rows"] == 1
    assert "malformed_record" in (tmp_path / "out/rejected_records.csv").read_text()

def test_unexpected_schema_fails_loudly(tmp_path):
    metrics, _, _ = run(tmp_path, [["1", "E1", "A100"]], headers=("event_id", "entity_id", "code"))
    assert metrics["status"] == "failed"
    assert "pipeline_failure" in (tmp_path / "out/rejected_records.csv").read_text()

def test_version_refresh_changes_accepted_codes(tmp_path):
    metrics, _, _ = run(tmp_path, [["1", "E1", "F600", "2026-10-01"]], version="v2")
    assert metrics["accepted_rows"] == 1

def test_rerun_is_idempotent(tmp_path):
    metrics1, source, db = run(tmp_path, [["1", "E1", "A100", "2026-10-01"]])
    metrics2 = run_pipeline(source, REF, db, tmp_path / "out", "v1")
    import sqlite3
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM accepted_records WHERE run_id=?", (metrics1["run_id"],)).fetchone()[0]
    assert metrics1["run_id"] == metrics2["run_id"]
    assert count == 1

def test_missing_input_file_fails_with_reason(tmp_path):
    metrics = run_pipeline(tmp_path / "absent.csv", REF, tmp_path / "db.sqlite", tmp_path / "out")
    assert metrics["status"] == "failed"
    assert metrics["failure_reason"]
    
def test_effective_date_boundaries_are_inclusive(tmp_path):
    metrics, _, _ = run(tmp_path, [
        ["1", "E1", "B200", "2025-01-01"],
        ["2", "E2", "B200", "2026-12-31"],
    ], version="v1")

    assert metrics["status"] == "success"
    assert metrics["accepted_rows"] == 2
    assert metrics["rejected_rows"] == 0

