"""Command-line interface for reproducible local refreshes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .contracts import TABLE_FILES
from .pipeline import build_and_promote
from .validation import validate_tables


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="macro-rus")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Ingest, validate and promote a vintage")
    build.add_argument("--raw-dir", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    validate = subparsers.add_parser("validate", help="Validate promoted Parquet files")
    validate.add_argument("--data-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "build":
        result = build_and_promote(args.raw_dir, args.output_dir)
        print(json.dumps(result.manifest, ensure_ascii=False, indent=2))
        return
    tables = {
        name: pd.read_parquet(args.data_dir / filename)
        for name, filename in TABLE_FILES.items()
        if name != "quality_events" and (args.data_dir / filename).exists()
    }
    result = validate_tables(tables)
    print(result.events.to_string(index=False))
    if not result.is_valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

