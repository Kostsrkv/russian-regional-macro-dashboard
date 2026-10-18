"""Stage published January-to-month production in a fresh local candidate."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from macro_rus.cumulative_production import build_cumulative_candidate


def run(output):
    result = build_cumulative_candidate(ROOT, Path(output))
    print(json.dumps({k: result[k] for k in ["status", "blocking_failures", "rows", "regions", "periods",
                                           "earliest", "latest", "reported_rows", "missing_rows",
                                           "original_vintage_rows", "overlap_keys", "overlap_rows",
                                           "pilot_q1_handchecks", "dataset_sha256"]}, indent=2))
    return result


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else ROOT / "outputs/cumulative_production_candidate_2026-10-02")
