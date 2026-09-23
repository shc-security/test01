from __future__ import annotations

"""Strict PIT runner with coverage measured on the eligible non-financial universe.

The underlying scorer intentionally rejects financial firms because the LFS ROIC
model is not comparable for banks, insurers, brokers, cards, or fund vehicles.
Those structural exclusions must not count as failed score coverage. This runner
keeps the requested >=80% gate, but applies it to the denominator the research
specification actually defines: mapped corporations minus confirmed financial
exclusions.
"""

import argparse
import json
from argparse import Namespace

from backtest import kr_point_in_time as base
from backtest import kr_point_in_time_v2 as strict

# Historical names that are unambiguously financial but do not contain one of
# the generic financial-business tokens used by pit_dart.is_financial_company.
# Keep this list narrow so non-financial holding companies are not excluded.
EXPLICIT_FINANCIAL_NAMES = {
    "신한지주",
    "현대해상",
}


def strict_snapshot_score(dart, corp, ticker, asof, cap_snapshot):
    name = str(corp.get("corp_name") or "").replace(" ", "")
    if name in EXPLICIT_FINANCIAL_NAMES:
        raise ValueError("financial-sector company excluded: ROIC model is not comparable")
    return strict.strict_snapshot_score(dart, corp, ticker, asof, cap_snapshot)


def validate_eligible_coverage(start_year: int, end_year: int, minimum: float) -> None:
    failures = []
    for year in range(start_year, end_year + 1):
        path = base.OUT_DIR / f"audit_{year}.json"
        if not path.exists():
            failures.append(f"{year}: audit missing")
            continue
        audit = json.loads(path.read_text(encoding="utf-8"))
        mapped = int(audit.get("mapped_corporations") or 0)
        scored = int(audit.get("scored_corporations") or 0)
        counts = audit.get("error_counts") or {}
        financial = int(counts.get("financial_sector_excluded") or 0)
        eligible = mapped - financial
        coverage = scored / eligible if eligible > 0 else 0.0
        audit["financial_sector_exclusions"] = financial
        audit["eligible_nonfinancial_corporations"] = eligible
        audit["eligible_score_coverage"] = coverage
        audit["eligible_score_coverage_gate"] = minimum
        path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"{year}: eligible non-financial coverage {scored}/{eligible} = {coverage:.1%} "
            f"(financial exclusions={financial})",
            flush=True,
        )
        if coverage < minimum:
            failures.append(f"{year}: eligible score coverage {coverage:.1%} < {minimum:.1%}")
    if failures:
        raise RuntimeError("; ".join(failures))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start-year", type=int, default=2016)
    p.add_argument("--end-year", type=int, default=2025)
    p.add_argument("--universe-size", type=int, default=100)
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--min-mapping-coverage", type=float, default=0.95)
    p.add_argument("--min-score-coverage", type=float, default=0.80)
    args = p.parse_args()

    base.snapshot_score = strict_snapshot_score
    # Disable only the legacy all-mapped denominator gate. The same requested
    # threshold is enforced below on the eligible non-financial denominator.
    run_args = Namespace(**vars(args))
    run_args.min_score_coverage = 0.0
    base.run(run_args)
    validate_eligible_coverage(args.start_year, args.end_year, args.min_score_coverage)


if __name__ == "__main__":
    main()
