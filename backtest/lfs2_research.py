"""LFS 2.0 research harness.

Runs after the strict PIT backtest and consumes its historical cohort outputs.
It deliberately does not publish a winning model: candidates must be evaluated
out-of-sample before any UI promotion.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
RESEARCH_VERSION = 1


def main() -> None:
    candidates = {
        "lfs": ["lfs"],
        "lfs_value": ["lfs", "value"],
        "lfs_momentum": ["lfs", "momentum"],
        "lfs_value_momentum": ["lfs", "value", "momentum"],
        "lfs_value_momentum_earnings": ["lfs", "value", "momentum", "earnings_revision"],
    }
    manifest = {
        "status": "research_started",
        "research_version": RESEARCH_VERSION,
        "candidates": candidates,
        "portfolio_sizes": [5, 10, 20],
        "rebalance": ["quarterly", "semiannual", "annual"],
        "required_metrics": ["cagr", "benchmark_excess", "mdd", "sharpe", "hit_rate", "spearman_ic", "sample_count"],
        "rules": [
            "point_in_time_inputs_only",
            "no_current_data_backfill",
            "no_threshold_relaxation",
            "out_of_sample_validation_required",
            "no_ui_publication_until_validated",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "lfs2_research_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
