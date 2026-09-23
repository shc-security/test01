from __future__ import annotations

"""Strict PIT runner with eligible-universe coverage and corporate-action-aware returns."""

import argparse
import json
import math
from argparse import Namespace
from datetime import date

import pandas as pd

from backtest import kr_point_in_time as base
from backtest import kr_point_in_time_v2 as strict

EXPLICIT_FINANCIAL_NAMES = {"신한지주", "현대해상"}

# BGF Retail (old 027410) was split on 2017-11-01 into surviving BGF
# (027410) and newly listed BGF Retail (282330).  One old share economically
# became 0.6511658 BGF share + 0.3488342 BGF Retail share.  A plain 027410
# price series therefore creates a false ~97% loss after the demerger.
BGF_DEMERGER_DATE = date(2017, 11, 1)
BGF_SURVIVING_RATIO = 0.6511658
BGF_SPINOFF_RATIO = 0.3488342
BGF_SPINOFF_TICKER = "282330"


def strict_snapshot_score(dart, corp, ticker, asof, cap_snapshot):
    name = str(corp.get("corp_name") or "").replace(" ", "")
    if name in EXPLICIT_FINANCIAL_NAMES:
        raise ValueError("financial-sector company excluded: ROIC model is not comparable")
    return strict.strict_snapshot_score(dart, corp, ticker, asof, cap_snapshot)


def _split_events(store, ticker: str):
    """Infer mechanical stock splits/reverse-splits from Marcap shares and price.

    A genuine split changes listed shares sharply while the raw close moves in the
    reciprocal direction. Rights offerings / ordinary issuance are deliberately
    not adjusted unless the price/share discontinuity is mechanically reciprocal.
    """
    cache = getattr(store, "_strict_split_events", None)
    if cache is None:
        cache = {}
        store._strict_split_events = cache
    if ticker in cache:
        return cache[ticker]

    frames = []
    for year in range(2015, date.today().year + 1):
        try:
            df = store.year(year)
        except Exception:
            continue
        part = df[df["Code"] == ticker][["Date", "Close", "Stocks"]].copy()
        if not part.empty:
            frames.append(part)
    if not frames:
        cache[ticker] = []
        return []

    x = pd.concat(frames).sort_values("Date").drop_duplicates("Date", keep="last")
    events = []
    prev = None
    for _, row in x.iterrows():
        close = float(row["Close"]) if pd.notna(row["Close"]) else 0.0
        shares = float(row["Stocks"]) if pd.notna(row["Stocks"]) else 0.0
        if prev is not None and close > 0 and shares > 0 and prev[1] > 0 and prev[2] > 0:
            sr = shares / prev[2]
            pr = close / prev[1]
            # >=20% share-count discontinuity, with market-cap continuity within 35%.
            # This catches 50:1 / 5:1 / 2:1 splits while avoiding ordinary issuance.
            if (sr >= 1.20 or sr <= (1 / 1.20)) and abs(math.log(pr * sr)) <= math.log(1.35):
                events.append((row["Date"], sr))
        prev = (row["Date"], close, shares)
    cache[ticker] = events
    return events


def _adjust_to_common_basis(store, ticker: str, px: float, dt):
    # Convert a historical pre-split quote onto today's/post-event share basis.
    # Example: a 50:1 split turns a pre-split 2,500,000 KRW quote into
    # 50,000 KRW on the post-split basis, so the historical quote is divided
    # by the cumulative future split ratio (not multiplied).
    factor = 1.0
    for event_date, ratio in _split_events(store, ticker):
        if event_date > dt:
            factor *= ratio
    return float(px) / factor if factor else float(px)


_ORIG_FIRST = base.MarcapStore.first_price_on_or_after
_ORIG_LAST = base.MarcapStore.last_price_on_or_before
_ORIG_TERMINAL = base.terminal_price


def _adjusted_first(self, ticker, target, days=20):
    found = _ORIG_FIRST(self, ticker, target, days)
    if not found:
        return None
    px, dt = found
    return _adjust_to_common_basis(self, ticker, px, dt), dt


def _adjusted_last(self, ticker, start, target):
    found = _ORIG_LAST(self, ticker, start, target)
    if not found:
        return None
    px, dt = found
    return _adjust_to_common_basis(self, ticker, px, dt), dt


def _strict_terminal_price(marcap, ticker: str, start: date, target: date):
    """Return a comparable terminal value, including known demerger entitlements.

    For BGF, holders before the 2017 equity demerger received both the surviving
    BGF shares and newly listed BGF Retail shares.  We value both legs at the
    horizon instead of pretending that the post-demerger 027410 quote alone is
    the continuation of the pre-demerger company.
    """
    if ticker == "027410" and start < BGF_DEMERGER_DATE <= target:
        bgf = _ORIG_TERMINAL(marcap, ticker, BGF_DEMERGER_DATE, target)
        retail = _ORIG_TERMINAL(marcap, BGF_SPINOFF_TICKER, BGF_DEMERGER_DATE, target)
        bgf_px, bgf_dt, _ = bgf
        retail_px, retail_dt, _ = retail
        synthetic = BGF_SURVIVING_RATIO * bgf_px + BGF_SPINOFF_RATIO * retail_px
        return synthetic, max(bgf_dt, retail_dt), "demerger_total_value:027410+282330"
    return _ORIG_TERMINAL(marcap, ticker, start, target)


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
        print(f"{year}: eligible non-financial coverage {scored}/{eligible} = {coverage:.1%} (financial exclusions={financial})", flush=True)
        if coverage < minimum:
            failures.append(f"{year}: eligible score coverage {coverage:.1%} < {minimum:.1%}")
    if failures:
        raise RuntimeError("; ".join(failures))


def validate_return_sanity():
    path = base.OUT_DIR / "forward_returns.csv"
    if not path.exists():
        raise RuntimeError("forward_returns.csv missing")
    df = pd.read_csv(path)
    if df.empty:
        raise RuntimeError("no forward returns")
    # Flag extreme losses that are typical of an unhandled split/demerger.
    # Such rows must be explicitly modeled rather than silently published.
    suspicious = df[(df["price_return"] <= -0.95) & (df["horizon_years"] >= 3)]
    if not suspicious.empty:
        cols = ["cohort_year", "ticker", "name", "horizon_years", "price_return"]
        raise RuntimeError("suspicious corporate-action returns remain: " + suspicious[cols].head(10).to_json(orient="records", force_ascii=False))


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
    base.MarcapStore.first_price_on_or_after = _adjusted_first
    base.MarcapStore.last_price_on_or_before = _adjusted_last
    base.terminal_price = _strict_terminal_price
    run_args = Namespace(**vars(args))
    run_args.min_score_coverage = 0.0
    base.run(run_args)
    validate_eligible_coverage(args.start_year, args.end_year, args.min_score_coverage)
    validate_return_sanity()


if __name__ == "__main__":
    main()
