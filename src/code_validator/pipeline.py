from __future__ import annotations

import csv
import hashlib
import json
import logging
import sqlite3
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
INPUT_FIELDS = {"event_id", "entity_id", "code", "event_date"}
REFERENCE_FIELDS = {"reference_version", "code", "description", "valid_from", "valid_to", "deprecated"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_run_id(input_path: Path, reference_version: str) -> str:
    digest = hashlib.sha256()
    if input_path.exists():
        digest.update(input_path.read_bytes())
    else:
        # Still create a stable run record for a missing-file failure.
        digest.update(("MISSING:" + str(input_path.resolve())).encode("utf-8"))
    digest.update(b"\0")
    digest.update(reference_version.encode("utf-8"))
    return digest.hexdigest()[:16]


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Read a CSV while preserving malformed rows with extra columns for quarantine."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        rows: list[dict[str, Any]] = []
        for line_number, row in enumerate(reader, start=2):
            row["_line_number"] = line_number
            if None in row:
                row["_extra_values"] = row.pop(None)
            rows.append(row)
        return fields, rows


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS run_history (
            run_id TEXT PRIMARY KEY,
            source_file TEXT NOT NULL,
            reference_version TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            status TEXT NOT NULL,
            input_rows INTEGER NOT NULL,
            accepted_rows INTEGER NOT NULL,
            rejected_rows INTEGER NOT NULL,
            duration_ms REAL NOT NULL,
            failure_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS accepted_records (
            run_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            code TEXT NOT NULL,
            event_date TEXT NOT NULL,
            reference_version TEXT NOT NULL,
            reference_description TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_line INTEGER NOT NULL,
            PRIMARY KEY (run_id, event_id)
        );
        CREATE TABLE IF NOT EXISTS rejected_records (
            run_id TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_line INTEGER NOT NULL,
            event_id TEXT,
            entity_id TEXT,
            code TEXT,
            event_date TEXT,
            reason_code TEXT NOT NULL,
            reason_detail TEXT NOT NULL,
            raw_record TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_accepted_code_date ON accepted_records(code, event_date);
        CREATE INDEX IF NOT EXISTS idx_rejected_run ON rejected_records(run_id);
        """)


def load_reference(path: Path, reference_version: str) -> dict[str, list[dict[str, str]]]:
    fields, rows = read_csv(path)
    missing = REFERENCE_FIELDS - set(fields)
    if missing:
        raise ValueError(f"Reference schema missing required columns: {', '.join(sorted(missing))}")
    selected: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if row.get("reference_version") != reference_version:
            continue
        code = (row.get("code") or "").strip()
        if not code:
            raise ValueError(f"Reference row {row['_line_number']} has an empty code")
        try:
            start = parse_date(row["valid_from"])
            end = parse_date(row["valid_to"]) if row.get("valid_to") else None
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid effective date in reference row {row['_line_number']}: {exc}") from exc
        if end and end < start:
            raise ValueError(f"Reference row {row['_line_number']} has valid_to before valid_from")
        normalized = dict(row)
        normalized["code"] = code
        normalized["valid_from"] = start.isoformat()
        normalized["valid_to"] = end.isoformat() if end else ""
        normalized["deprecated"] = (row.get("deprecated") or "false").strip().lower()
        if normalized["deprecated"] not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError(f"Invalid deprecated flag in reference row {row['_line_number']}")
        selected.setdefault(code, []).append(normalized)
    # Prevent ambiguous overlapping validity windows for a code within a reference version.
    for code, versions in selected.items():
        ordered = sorted(versions, key=lambda r: r["valid_from"])
        for previous, current in zip(ordered, ordered[1:]):
            previous_end = previous["valid_to"] or "9999-12-31"
            if current["valid_from"] <= previous_end:
                raise ValueError(f"Overlapping effective-date ranges for code {code} in {reference_version}")
    return selected


def classify(row: dict[str, Any], refs: dict[str, list[dict[str, str]]]) -> tuple[str | None, str | None, dict[str, str] | None]:
    if row.get("_extra_values"):
        return "malformed_record", "Row contains more values than the CSV header", None
    missing = [field for field in sorted(INPUT_FIELDS) if row.get(field) is None or not str(row.get(field)).strip()]
    if missing:
        return "missing_required_field", f"Missing required field(s): {', '.join(missing)}", None
    event_id = str(row["event_id"]).strip()
    code = str(row["code"]).strip()
    try:
        event_date = parse_date(str(row["event_date"]).strip())
    except ValueError:
        return "invalid_event_date", "event_date must be an ISO-8601 date (YYYY-MM-DD)", None
    matches = refs.get(code)
    if not matches:
        return "unknown_code", f"Code {code!r} is absent from the selected reference version", None
    match = next((ref for ref in matches if ref["valid_from"] <= event_date.isoformat() and (not ref["valid_to"] or event_date.isoformat() <= ref["valid_to"])), None)
    if match is None:
        return "outside_effective_dates", f"Code {code!r} is not effective on {event_date.isoformat()}", None
    if match["deprecated"].lower() in {"true", "1", "yes"}:
        return "deprecated_code", f"Code {code!r} is deprecated for the applicable effective period", None
    return None, None, match


def run_pipeline(input_path: Path, reference_path: Path, db_path: Path, output_dir: Path, reference_version: str = "v1") -> dict[str, Any]:
    started = utc_now()
    start_clock = time.perf_counter()
    input_path, reference_path, db_path, output_dir = map(Path, (input_path, reference_path, db_path, output_dir))
    run_id = stable_run_id(input_path, reference_version)
    source_file = input_path.name
    init_db(db_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Starting run_id=%s source_file=%s reference_version=%s", run_id, source_file, reference_version)
    input_rows: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    status = "success"
    failure_reason = None
    try:
        fields, input_rows = read_csv(input_path)
        missing_columns = INPUT_FIELDS - set(fields)
        if missing_columns:
            raise ValueError(f"Input schema missing required columns: {', '.join(sorted(missing_columns))}")
        refs = load_reference(reference_path, reference_version)
        seen_event_ids: set[str] = set()
        for row in input_rows:
            line = int(row["_line_number"])
            raw = {k: v for k, v in row.items() if not k.startswith("_")}
            event_id = (row.get("event_id") or "").strip() or None
            if event_id and event_id in seen_event_ids:
                reason_code, detail, match = "duplicate_event_id", f"Duplicate event_id {event_id!r} in this batch", None
            else:
                if event_id:
                    seen_event_ids.add(event_id)
                reason_code, detail, match = classify(row, refs)
            if reason_code:
                rejected.append({"run_id": run_id, "source_file": source_file, "source_line": line, "event_id": event_id,
                                 "entity_id": (row.get("entity_id") or "").strip() or None, "code": (row.get("code") or "").strip() or None,
                                 "event_date": (row.get("event_date") or "").strip() or None, "reason_code": reason_code,
                                 "reason_detail": detail or "", "raw_record": json.dumps(raw, sort_keys=True)})
            else:
                accepted.append({"run_id": run_id, "event_id": event_id, "entity_id": str(row["entity_id"]).strip(),
                                 "code": str(row["code"]).strip(), "event_date": str(row["event_date"]).strip(),
                                 "reference_version": reference_version, "reference_description": match.get("description", ""),
                                 "source_file": source_file, "source_line": line})
    except Exception as exc:
        status = "failed"
        failure_reason = f"{type(exc).__name__}: {exc}"
        LOGGER.exception("Pipeline failed run_id=%s", run_id)
        # Fail fast on a broken file/schema/reference rather than silently treating all rows as valid.
        accepted = []
        rejected = [{"run_id": run_id, "source_file": source_file, "source_line": 0, "event_id": None, "entity_id": None,
                     "code": None, "event_date": None, "reason_code": "pipeline_failure", "reason_detail": failure_reason,
                     "raw_record": "{}"}]
    ended = utc_now()
    duration_ms = round((time.perf_counter() - start_clock) * 1000, 3)
    with sqlite3.connect(db_path) as conn:
        # Replacing records for the deterministic run_id makes reruns idempotent.
        conn.execute("DELETE FROM accepted_records WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM rejected_records WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM run_history WHERE run_id = ?", (run_id,))
        conn.executemany("""INSERT INTO accepted_records VALUES (:run_id,:event_id,:entity_id,:code,:event_date,:reference_version,:reference_description,:source_file,:source_line)""", accepted)
        conn.executemany("""INSERT INTO rejected_records VALUES (:run_id,:source_file,:source_line,:event_id,:entity_id,:code,:event_date,:reason_code,:reason_detail,:raw_record)""", rejected)
        conn.execute("""INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (run_id, source_file, reference_version, started, ended, status,
                     len(input_rows), len(accepted), len(rejected), duration_ms, failure_reason))
    write_outputs(output_dir, run_id, accepted, rejected)
    metrics = {"run_id": run_id, "source_file": source_file, "reference_version": reference_version, "started_at": started,
               "ended_at": ended, "status": status, "input_rows": len(input_rows), "accepted_rows": len(accepted),
               "rejected_rows": len(rejected), "duration_ms": duration_ms, "failure_reason": failure_reason,
               "rejects_by_reason": dict(Counter(r["reason_code"] for r in rejected))}
    (output_dir / "run_report.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    LOGGER.info("Finished run_id=%s status=%s input=%d accepted=%d rejected=%d duration_ms=%.3f", run_id, status, len(input_rows), len(accepted), len(rejected), duration_ms)
    return metrics


def write_outputs(output_dir: Path, run_id: str, accepted: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> None:
    accepted_fields = ["run_id", "event_id", "entity_id", "code", "event_date", "reference_version", "reference_description", "source_file", "source_line"]
    rejected_fields = ["run_id", "source_file", "source_line", "event_id", "entity_id", "code", "event_date", "reason_code", "reason_detail", "raw_record"]
    for filename, fields, rows in [("accepted_records.csv", accepted_fields, accepted), ("rejected_records.csv", rejected_fields, rejected)]:
        with (output_dir / filename).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def reconcile(db_path: Path, run_id: str) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT input_rows, accepted_rows, rejected_rows FROM run_history WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown run_id: {run_id}")
        input_rows, accepted_rows, rejected_rows = map(int, row)
        if input_rows != accepted_rows + rejected_rows:
            raise AssertionError(f"Count reconciliation failed: {input_rows} != {accepted_rows} + {rejected_rows}")
        actual_accepted = conn.execute("SELECT COUNT(*) FROM accepted_records WHERE run_id=?", (run_id,)).fetchone()[0]
        actual_rejected = conn.execute("SELECT COUNT(*) FROM rejected_records WHERE run_id=?", (run_id,)).fetchone()[0]
        if actual_accepted != accepted_rows or actual_rejected != rejected_rows:
            raise AssertionError("Persisted record counts do not match run metrics")
    return {"input_rows": input_rows, "accepted_rows": accepted_rows, "rejected_rows": rejected_rows,
            "reconciled": input_rows == accepted_rows + rejected_rows}
