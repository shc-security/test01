from __future__ import annotations

"""Strict PIT runner with eligible-universe coverage and corporate-action-aware returns."""

import argparse
import json
from argparse import Namespace
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from backtest import kr_point_in_time as base
from backtest import kr_point_in_time_v2 as strict
from backtest.pit_dart import is_financial_company

EXPLICIT_FINANCIAL_NAMES = {"신한지주", "현대해상"}
_PRICE_CACHE = {}


def strict_snapshot_score(dart, corp, ticker, asof, cap_snapshot):
    name = str(corp.get("corp_name") or "").replace(" ", "")
    if name in EXPLICIT_FINANCIAL_NAMES or is_financial_company(None, name):
        raise ValueError("financial-sector company excluded: ROIC model is not comparable")
    return strict.strict_snapshot_score(dart, corp, ticker, asof, cap_snapshot)


def _yahoo_symbol(ticker: str, market: str | None = None) -> str:
    suffix = ".KQ" if str(market or "").upper() == "KOSDAQ" else ".KS"
    return f"{str(ticker).zfill(6)}{suffix}"


def _yahoo_adjusted_price(ticker: str, target: date, market: str | None = None, on_or_after: bool = True):
    """Return Yahoo adjusted close near target.

    Adjusted close is used specifically to put pre/post split quotes on one basis.
    The selected trading date remains point-in-time; only the price scale is adjusted.
    """
    key = (str(ticker).zfill(6), str(market or ""), target.isoformat(), bool(on_or_after))
    if key in _PRICE_CACHE:
        return _PRICE_CACHE[key]
    symbol = _yahoo_symbol(ticker, market)
    lo = target - timedelta(days=12 if not on_or_after else 2)
    hi = target + timedelta(days=22)
    params = {
        "period1": int(datetime.combine(lo, datetime.min.time()).timestamp()),
        "period2": int(datetime.combine(hi, datetime.min.time()).timestamp()),
        "interval": "1d",
        "events": "history,div,splits",
    }
    try:
        r = requests.get(
            base.YAHOO_CHART.format(symbol=symbol),
            params=params,
            headers={"User-Agent": "Mozilla/5.0 lattice-stock-analyzer-backtest"},
            timeout=(4, 15),
        )
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        adj = result.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose") or []
        raw = result.get("indicators", {}).get("quote", [{}])[0].get("close") or []
        values = adj if any(x is not None for x in adj) else raw
        pairs = []
        for ts, px in zip(timestamps, values):
            if px is None:
                continue
            d = datetime.utcfromtimestamp(ts).date()
            pairs.append((d, float(px)))
        pairs.sort()
        if on_or_after:
            eligible = [x for x in pairs if x[0] >= target]
            out = (eligible[0][1], eligible[0][0]) if eligible else None
        else:
            eligible = [x for x in pairs if x[0] <= target]
            out = (eligible[-1][1], eligible[-1][0]) if eligible else None
    except Exception:
        out = None
    _PRICE_CACHE[key] = out
    return out


def strict_start_price(marcap, ticker: str, asof: date):
    # Resolve the historical market from the PIT snapshot, then use adjusted close.
    market = None
    try:
        _, snap = marcap.snapshot_on_or_after(asof)
        hit = snap[snap["Code"] == str(ticker).zfill(6)]
        if not hit.empty:
            market = str(hit.iloc[0].get("Market") or "")
    except Exception:
        pass
    found = _yahoo_adjusted_price(ticker, asof, market, True)
    if found:
        return found
    return base.MarcapStore.first_price_on_or_after(marcap, str(ticker).zfill(6), asof, days=20)


def strict_terminal_price(marcap, ticker: str, start: date, target: date):
    market = None
    try:
        _, snap = marcap.snapshot_on_or_after(start)
        hit = snap[snap["Code"] == str(ticker).zfill(6)]
        if not hit.empty:
            market = str(hit.iloc[0].get("Market") or "")
    except Exception:
        pass
    found = _yahoo_adjusted_price(ticker, target, market, True)
    if found:
        px, dt = found
        return px, dt, "yahoo_adjusted_target_or_next"
    # Fail-safe for delisted/suspended names: retain them rather than silently
    # dropping them. Such rows are explicitly labelled for downstream review.
    return base.terminal_price(marcap, str(ticker).zfill(6), start, target)


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


def validate_receipt_audit(start_year: int, end_year: int, minimum_spotchecks: int = 10) -> None:
    path = base.OUT_DIR / "ranked_cohorts.csv"
    if not path.exists():
        raise RuntimeError("ranked_cohorts.csv missing")
    df = pd.read_csv(path)
    if df.empty:
        raise RuntimeError("no ranked cohorts")
    checked = 0
    violations = []
    for _, row in df.iterrows():
        asof = str(row.get("asof_date") or "")[:10].replace("-", "")
        raw = row.get("filing_dates_used")
        if pd.isna(raw):
            continue
        try:
            dates = json.loads(raw) if isinstance(raw, str) else list(raw)
        except Exception:
            dates = [x.strip().strip("[]'\"") for x in str(raw).split(",") if x.strip()]
        dates = [str(x).replace("-", "") for x in dates if str(x).strip()]
        if dates:
            checked += 1
            late = [d for d in dates if len(d) >= 8 and d[:8] > asof]
            if late:
                violations.append((row.get("cohort_year"), row.get("ticker"), late, asof))
    if violations:
        raise RuntimeError("look-ahead filing dates remain: " + repr(violations[:10]))
    if checked < minimum_spotchecks:
        raise RuntimeError(f"receipt audit rows {checked} < required {minimum_spotchecks}")
    print(f"receipt audit: {checked} ranked rows traced to filing dates on/before cohort as-of dates", flush=True)


def validate_return_sanity():
    path = base.OUT_DIR / "forward_returns.csv"
    if not path.exists():
        raise RuntimeError("forward_returns.csv missing")
    df = pd.read_csv(path)
    if df.empty:
        raise RuntimeError("no forward returns")
    suspicious = df[((df["price_return"] <= -0.95) | (df["price_return"] >= 20.0)) & (df["horizon_years"] >= 3)]
    if not suspicious.empty:
        cols = ["cohort_year", "ticker", "name", "horizon_years", "start_price", "end_price", "price_return", "terminal_source"]
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
    base.start_price = strict_start_price
    base.terminal_price = strict_terminal_price
    run_args = Namespace(**vars(args))
    run_args.min_score_coverage = 0.0
    base.run(run_args)
    validate_eligible_coverage(args.start_year, args.end_year, args.min_score_coverage)
    validate_receipt_audit(args.start_year, args.end_year, minimum_spotchecks=10)
    validate_return_sanity()


if __name__ == "__main__":
    main()
