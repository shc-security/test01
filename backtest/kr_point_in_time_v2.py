from __future__ import annotations

"""Strict point-in-time wrapper around kr_point_in_time.

Historical LFS inputs come only from exact OpenDART annual-report receipts that
were public by the ranking date. Later restatements from today's financial API
are never used as historical facts.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime

from api import analyze as lfs
from backtest import kr_point_in_time as base
from backtest.pit_dart import HistoricalDart, is_financial_company

FLOW_TAGS = {
    "revenue": ("RevenueFromContractsWithCustomers", "Revenue", "Sales", "SalesRevenue", "OperatingRevenue", "RevenueFromRenderingOfServices", "Revenues"),
    "operating_income": ("OperatingIncomeLoss", "OperatingProfitLoss", "OperatingIncome", "OperatingProfit"),
    "net_income": ("ProfitLoss", "ProfitLossAttributableToOwnersOfParent", "NetIncomeLoss", "NetIncome"),
    "cfo": ("CashFlowsFromUsedInOperatingActivities", "NetCashFlowsFromUsedInOperatingActivities"),
    "capex": ("PurchaseOfPropertyPlantAndEquipment", "PaymentsToAcquirePropertyPlantAndEquipment", "AcquisitionOfPropertyPlantAndEquipment", "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssets"),
}
BALANCE_TAGS = {
    "assets": ("Assets", "TotalAssets"),
    "equity": ("Equity", "EquityAttributableToOwnersOfParent", "TotalEquity"),
    "cash": ("CashAndCashEquivalents", "CashAndCashEquivalentsAtEndOfPeriodCf"),
}
DEBT_TAGS = ("ShorttermBorrowings", "BorrowingsCurrent", "CurrentPortionOfLongtermBorrowings", "LongtermBorrowings", "CurrentPortionOfBonds", "BondsIssued")


def _iso(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except Exception:
        return None


def _source_groups(facts, year):
    groups = defaultdict(list)
    for f in facts:
        end = _iso(f.get("end") or f.get("instant"))
        if end and end.year == year:
            groups[f.get("source_file") or ""].append(f)
    return groups


def _tag_matches(tag, aliases):
    if tag in aliases:
        return True
    low = str(tag or "").lower()
    strong = [a.lower() for a in aliases if len(a) >= 12]
    return any(a in low or low in a for a in strong)


def _flow_value(rows, tags, year):
    candidates = []
    for f in rows:
        if not _tag_matches(f.get("tag"), tags):
            continue
        st, en = _iso(f.get("start")), _iso(f.get("end"))
        if st and en and en.year == year and 330 <= (en - st).days <= 380:
            candidates.append(float(f["value"]))
    if not candidates:
        return None
    counts = Counter(candidates)
    return sorted(counts, key=lambda v: (-counts[v], -abs(v), v))[0]


def _instant_value(rows, tags, year):
    candidates = []
    for f in rows:
        if not _tag_matches(f.get("tag"), tags):
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
        coverage = sum(m.get(k) is not None for k in ("revenue", "operating_income", "net_income", "assets", "equity", "cash", "cfo", "capex"))
        key = (coverage, abs(m.get("assets") or 0.0), source)
        if best is None or key > best_key:
            best, best_key = m, key
            best["_source_file"] = source
    if not best or best.get("revenue") is None or best.get("operating_income") is None:
        tags = sorted({str(f.get("tag") or "") for f in facts if (_iso(f.get("end") or f.get("instant")) and _iso(f.get("end") or f.get("instant")).year == year)})
        likely = [t for t in tags if any(k in t.lower() for k in ("revenue", "sales", "operat", "profit", "income"))][:30]
        raise ValueError(f"receipt XBRL metric parsing incomplete for FY{year}; likely_tags={likely}")
    best["year"] = year
    best["_fs_div"] = "receipt-xbrl"
    return best


def _latest_parseable_receipt(pit, corp_code, fy, asof):
    failures = []
    receipts = pit.annual_receipts(corp_code, fy, asof)
    for rec in receipts:
        rdt = str(rec.get("rcept_dt") or rec.get("rcept_no", "")[:8])
        if len(rdt) != 8 or not rdt.isdigit() or rdt > asof.strftime("%Y%m%d"):
            raise ValueError(f"look-ahead receipt detected FY{fy}: {rdt} > {asof:%Y%m%d}")
        try:
            facts = pit.xbrl_facts(rec["rcept_no"])
            metric = _metrics_from_receipt(facts, fy)
            return rec, metric, failures
        except (RuntimeError, ValueError) as exc:
            failures.append(f"{rec.get('rcept_no')}: {exc}")
    if not receipts:
        failures.append("no annual-report receipt found by as-of date")
    return None, None, failures


def strict_snapshot_score(dart_unused, corp, ticker, asof, cap_snapshot):
    pit = HistoricalDart(cache_dir=base.CACHE_DIR / "pit_dart")
    latest_year = asof.year - 1
    info = {}
    try:
        info = dart_unused.company(corp["corp_code"])
    except Exception:
        pass
    if is_financial_company(info.get("induty_code"), corp.get("corp_name")):
        raise ValueError("financial-sector company excluded: ROIC model is not comparable")

    rows, receipts = [], []
    parse_failures = {}
    for fy in range(latest_year, max(2011, latest_year - 6), -1):
        rec, metric, failures = _latest_parseable_receipt(pit, corp["corp_code"], fy, asof)
        if failures:
            parse_failures[fy] = failures
        if not rec or not metric:
            continue
        rdt = str(rec.get("rcept_dt") or rec.get("rcept_no", "")[:8])
        metric["_receipt_no"] = rec["rcept_no"]
        metric["_receipt_date"] = rdt
        rows.append(metric)
        receipts.append({"year": fy, "rcept_no": rec["rcept_no"], "rcept_dt": rdt, "source_file": metric.get("_source_file")})

    rows.sort(key=lambda x: x["year"])
    receipts.sort(key=lambda x: x["year"])
    if not rows or rows[-1]["year"] != latest_year:
        detail = parse_failures.get(latest_year, [])[:3]
        raise ValueError(f"latest annual statement is unavailable point-in-time; FY{latest_year} diagnostics={detail}")
    if len(rows) < 2:
        detail = {y: v[:2] for y, v in parse_failures.items() if y != latest_year}
        raise ValueError(f"insufficient historical financial periods; diagnostics={detail}")
    rows = rows[-6:]
    receipts = [r for r in receipts if r["year"] in {x["year"] for x in rows}]
    latest = rows[-1]

    cap_row = cap_snapshot.get(ticker)
    if not cap_row:
        raise ValueError("historical KRX market cap missing")
    sector = lfs._sector_from_kr_industry(info.get("induty_code"))
    if sector == "Financials":
        sector = "General"
    equity_cap = cap_row["market_cap"]
    ev = None
    if latest.get("debt") is not None and latest.get("cash") is not None:
        ev = equity_cap + latest["debt"] - latest["cash"]

    result = lfs._compute_lfs(rows, tax_rate=0.24, current=None, industry={"sector": sector}, equity_market_cap=equity_cap, enterprise_value=ev)
    return {
        "ticker": ticker, "name": corp["corp_name"], "corp_code": corp["corp_code"],
        "market": cap_row["market"], "score": result["score"],
        "current_score": result["current_score"], "long_score": result["normalized_score"],
        "valuation_available": result["valuation_available"], "history_periods": len(rows),
        "filing_dates_used": [r["rcept_dt"] for r in receipts], "receipts_used": receipts,
        "preferred_coverage_complete": False, "preferred_tickers_used": [],
        "market_cap": equity_cap, "enterprise_value": ev,
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
