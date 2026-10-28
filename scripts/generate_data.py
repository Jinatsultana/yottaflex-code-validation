#!/usr/bin/env python3
"""Generate a deterministic larger incoming dataset with labelled edge cases."""
import argparse, csv, random
from pathlib import Path

CODES = ["A100", "B200", "C300", "D400", "Z999", "F600"]
DATES = ["2026-10-01", "2026-12-31", "2027-01-01", "2024-12-31", "not-a-date"]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--output", type=Path, default=Path("data/input/generated_codes.csv"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.rows < 0: parser.error("--rows must be >= 0")
    rng = random.Random(args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["event_id", "entity_id", "code", "event_date"])
        for i in range(args.rows):
            # Most rows are ordinary; a fixed fraction intentionally exercises edge cases.
            code = rng.choices(CODES, weights=[55, 15, 5, 5, 10, 10], k=1)[0]
            event_date = rng.choices(DATES, weights=[60, 15, 10, 10, 5], k=1)[0]
            entity_id = "" if i % 997 == 0 else f"ENT-{i % 25000:05d}"
            event_id = f"EVT-{i:08d}"
            if i > 0 and i % 1000 == 0: event_id = f"EVT-{i-1:08d}"  # labelled duplicate key case
            writer.writerow([event_id, entity_id, code, event_date])
    print(f"Generated {args.rows} rows at {args.output} (seed={args.seed})")
if __name__ == "__main__": main()
