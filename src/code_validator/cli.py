from __future__ import annotations
import argparse
import json
import logging
from pathlib import Path
from .pipeline import reconcile, run_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate incoming codes against a versioned reference dataset.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run the validation pipeline")
    run.add_argument("--input", type=Path, default=Path("data/input/incoming_codes.csv"))
    run.add_argument("--reference", type=Path, default=Path("data/reference/reference_codes.csv"))
    run.add_argument("--db", type=Path, default=Path("outputs/validation.db"))
    run.add_argument("--output-dir", type=Path, default=Path("outputs"))
    run.add_argument("--reference-version", default="v1")
    check = sub.add_parser("reconcile", help="Verify source-to-target row counts")
    check.add_argument("--db", type=Path, default=Path("outputs/validation.db"))
    check.add_argument("--run-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "run":
        result = run_pipeline(args.input, args.reference, args.db, args.output_dir, args.reference_version)
        print(json.dumps(result, indent=2))
        if result["status"] != "success":
            raise SystemExit(1)
    else:
        print(json.dumps(reconcile(args.db, args.run_id), indent=2))

if __name__ == "__main__":
    main()
