from __future__ import annotations

"""Strict point-in-time wrapper around kr_point_in_time.

The legacy runner is retained for universe/price/return plumbing, but every
historical LFS score is replaced here with metrics extracted from the exact
OpenDART annual-report receipt that was public by the ranking date.  No
fnlttSinglAcnt(All) values are used for historical scoring.
"""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime

from api import analyze as lfs
from backtest import kr_point_in_time as base
from backtest.pit_dart import HistoricalDart, is_financial_company


FLOW_TAGS = {
    "revenue": ("RevenueFromContractsWithCustomers", "Revenue", "SalesRevenue", "OperatingRevenue"),
    "operating_income": ("OperatingIncomeLoss", "OperatingProfitLoss"),
    "net_income": ("ProfitLoss", "ProfitLossAttributableToOwnersOfParent"),
    "cfo": ("CashFlowsFromUsedInOperatingActivities",),
    "capex": ("PurchaseOfPropertyPlantAndEquipment", "PaymentsToAcquirePropertyPlantAndEquipment"),
}
BALANCE_TAGS = {
    "assets": ("Assets",),
    "equity": ("Equity", "EquityAttributableToOwnersOfParent"),
    "cash": ("CashAndCashEquivalents",),
}
DEBT_TAGS = (
    "ShorttermBorrowings", "BorrowingsCurrent", "CurrentPortionOfLongtermBorrowings",
    "LongtermBorrowings", "CurrentPortionOfBonds", "BondsIssued",
)


def _iso(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except Exception:
        return None


def _source_groups(facts, year):
    """Group facts by instance file and keep sources that contain FY facts."""
    groups = defaultdict(list)
    for f in facts:
        end = _iso(f.get("end") or f.get("instant"))
        if end and end.year == year:
            groups[f.get("source_file") or ""] .append(f)
    return groups


def _flow_value(rows, tags, year):
    candidates = []
    for f in rows:
        if f.get("tag") not in tags:
            continue
        st, en = _iso(f.get("start")), _iso(f.get("end"))
        if not st or not en or en.year != year:
            continue
        days = (en - st).days
        # Annual income/cash-flow contexts. Korean Dec-year issuers are normally 364/365 days.
        if 330 <= days <= 380:
            candidates.append(float(f["value"]))
    if not candidates:
        return None
    # Duplicate contexts (presentation/segment variants) are common. Prefer the modal
    # exact numeric value; ties resolve deterministically by absolute magnitude.
    counts = Counter(candidates)
    return sorted(counts, key=lambda v: (-counts[v], -abs(v), v))[0]


def _instant_value(rows, tags, year):
    candidates = []
    for f in rows:
        if f.get("tag") not in tags:
            continue
        inst = _iso(f.get("instant"))
        if inst and inst.year == year:
            candidates.append(float(f["value"]))
    if not candidates:
        return None
    counts = Counter(candidates)
    return sorted(counts, key=lambda v: (-counts[v], -abs(v), v))[0]


def _metrics_from_receipt(facts, year):
    best = None
    best_key = None
    for source, rows in _source_groups(facts, year).items():
        m = {k: _flow_value(rows, tags, year) for k, tags in FLOW_TAGS.items()}
        m.update({k: _instant_value(rows, tags, year) for k, tags in BALANCE_TAGS.items()})
        debt_parts = []
        for tag in DEBT_TAGS:
            v = _instant_value(rows, (tag,), year)
            if v is not None:
                debt_parts.append(v)
        m["debt"] = sum(debt_parts) if debt_parts else None
        if m.get("capex") is not None:
            m["capex"] = abs(m["capex"])
        # LFS needs revenue/op income plus balance-sheet support. Prefer the richest
        # instance; then prefer the larger asset base, which resolves CFS vs OFS in
        # the expected direction without consulting a later API snapshot.
        coverage = sum(m.get(k) is not None for k in ("revenue", "operating_income", "net_income", "assets", "equity", "cash", "cfo", "capex"))
        key = (coverage, abs(m.get("assets") or 0.0), source)
        if best is None or key > best_key:
            best, best_key = m, key
            best["_source_file"] = source
    if not best or best.get("revenue") is None or best.get("operating_income") is None:
        raise ValueError(f"receipt XBRL metric parsing incomplete for FY{year}")
    best["year"] = year
    best["_fs_div"] = "receipt-xbrl"
    return best


def strict_snapshot_score(dart_unused, corp, ticker, asof, cap_snapshot):
    pit = HistoricalDart(cache_dir=base.CACHE_DIR / "pit_dart")
    score_year = asof.year
    latest_year = score_year - 1
    rows = []
    receipts = []
    # Each year's metric comes from that year's own annual-report receipt, not a
    # comparative column in a later filing. This prevents later restatements from
    # rewriting the historical information set.
    for fy in range(max(2012, latest_year - 5), latest_year + 1):
        rec = pit.annual_receipt(corp["corp_code"], fy, asof)
        if not rec:
            continue
        rdt = str(rec.get("rcept_dt") or rec.get("rcept_no", "")[:8])
        if len(rdt) != 8 or rdt > asof.strftime("%Y%m%d"):
            raise ValueError(f"look-ahead receipt detected FY{fy}: {rdt} > {asof:%Y%m%d}")
        facts = pit.xbrl_facts(rec["rcept_no"])
        metric = _metrics_from_receipt(facts, fy)
        metric["_receipt_no"] = rec["rcept_no"]
        metric["_receipt_date"] = rdt
        rows.append(metric)
        receipts.append({"year": fy, "rcept_no": rec["rcept_no"], "rcept_dt": rdt, "source_file": metric.get("_source_file")})

    rows.sort(key=lambda x: x["year"])
    if not rows or rows[-1]["year"] != latest_year:
        raise ValueError("latest annual statement is unavailable point-in-time")
    if len(rows) < 2:
        raise ValueError("insufficient historical financial periods")
    rows = rows[-6:]
    latest = rows[-1]

    cap_row = cap_snapshot.get(ticker)
    if not cap_row:
        raise ValueError("historical KRX market cap missing")

    # Company metadata is used only to gate financial-sector comparability; no
    # financial amount is sourced from company.json.
    info = {}
    try:
        info = dart_unused.company(corp["corp_code"])
    except Exception:
        pass
    if is_financial_company(info.get("induty_code"), corp.get("corp_name")):
        raise ValueError("financial-sector company excluded: ROIC model is not comparable")
    sector = lfs._sector_from_kr_industry(info.get("induty_code"))
    if sector == "Financials":
        # Non-financial holding companies can carry a 64xxx code. Avoid applying
        # financial-sector benchmarks after they have passed the explicit gate.
        sector = "General"

    equity_cap = cap_row["market_cap"]
    ev = None
    if latest.get("debt") is not None and latest.get("cash") is not None:
        ev = equity_cap + latest["debt"] - latest["cash"]

    result = lfs._compute_lfs(
        rows,
        tax_rate=0.24,
        current=None,
        industry={"sector": sector},
        equity_market_cap=equity_cap,
        enterprise_value=ev,
    )
    return {
        "ticker": ticker,
        "name": corp["corp_name"],
        "corp_code": corp["corp_code"],
        "market": cap_row["market"],
        "score": result["score"],
        "current_score": result["current_score"],
        "long_score": result["normalized_score"],
        "valuation_available": result["valuation_available"],
        "history_periods": len(rows),
        "filing_dates_used": [r["rcept_dt"] for r in receipts],
        "receipts_used": receipts,
        "preferred_coverage_complete": False,
        "preferred_tickers_used": [],
        "market_cap": equity_cap,
        "enterprise_value": ev,
        "point_in_time_source": "receipt-specific OpenDART XBRL",
    }


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
    base.run(args)


if __name__ == "__main__":
    main()
