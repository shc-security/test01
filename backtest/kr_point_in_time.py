from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import zipfile
from dataclasses import asdict
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api import analyze as lfs
from backtest.core import HORIZONS, Observation, assert_point_in_time, rank_scores, summarize_rank_observations


DART_BASE = "https://opendart.fss.or.kr/api"
CACHE_DIR = ROOT / "backtest" / ".cache"
OUT_DIR = ROOT / "backtest" / "out"
MARCAP_URL = "https://raw.githubusercontent.com/FinanceData/marcap/master/data/marcap-{year}.parquet"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _json_load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _retry(fn, attempts=4, base_sleep=1.0):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(base_sleep * (2 ** i))
    raise last


class DartCache:
    def __init__(self, api_key: str):
        if not api_key:
            raise RuntimeError("DART_API_KEY is required for the point-in-time backtest.")
        self.key = api_key
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "lattice-stock-analyzer-backtest/0.1"})
        self.root = CACHE_DIR / "dart"
        self.root.mkdir(parents=True, exist_ok=True)

    def _get_json(self, endpoint: str, params: dict, cache_key: str) -> dict:
        path = self.root / f"{cache_key}.json"
        if path.exists():
            return _json_load(path)

        def call():
            r = self.session.get(
                f"{DART_BASE}/{endpoint}",
                params={"crtfc_key": self.key, **params},
                timeout=(5, 25),
            )
            r.raise_for_status()
            return r.json()

        data = _retry(call)
        status = data.get("status")
        if status not in (None, "000", "013"):
            raise RuntimeError(f"OpenDART {endpoint} error {status}: {data.get('message')}")
        _json_dump(path, data)
        time.sleep(0.06)
        return data

    def corp_codes(self) -> list[dict]:
        path = self.root / "corp_codes.json"
        if path.exists():
            return _json_load(path)

        def call():
            r = self.session.get(
                f"{DART_BASE}/corpCode.xml",
                params={"crtfc_key": self.key},
                timeout=(5, 30),
            )
            r.raise_for_status()
            return r.content

        raw = _retry(call)
        try:
            zf = zipfile.ZipFile(BytesIO(raw))
            xml_bytes = zf.read(zf.namelist()[0])
        except zipfile.BadZipFile as exc:
            raise RuntimeError("OpenDART corpCode.xml download was not a ZIP.") from exc

        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_bytes)
        rows = []
        for item in root.findall("list"):
            rows.append(
                {
                    "corp_code": (item.findtext("corp_code") or "").strip(),
                    "corp_name": (item.findtext("corp_name") or "").strip(),
                    "stock_code": (item.findtext("stock_code") or "").strip(),
                }
            )
        _json_dump(path, rows)
        return rows

    def full(self, corp_code: str, year: int, reprt_code: str) -> tuple[list[dict] | None, str | None]:
        for fs_div in ("CFS", "OFS"):
            data = self._get_json(
                "fnlttSinglAcntAll.json",
                {
                    "corp_code": corp_code,
                    "bsns_year": str(year),
                    "reprt_code": reprt_code,
                    "fs_div": fs_div,
                },
                f"full_{corp_code}_{year}_{reprt_code}_{fs_div}",
            )
            rows = data.get("list") or []
            if rows:
                return rows, fs_div
        return None, None

    def major(self, corp_code: str, year: int, reprt_code: str) -> tuple[list[dict] | None, str | None]:
        data = self._get_json(
            "fnlttSinglAcnt.json",
            {
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": reprt_code,
            },
            f"major_{corp_code}_{year}_{reprt_code}",
        )
        rows = data.get("list") or []
        if not rows:
            return None, None
        cfs = [r for r in rows if r.get("fs_div") == "CFS"]
        ofs = [r for r in rows if r.get("fs_div") == "OFS"]
        if cfs:
            return cfs, "CFS"
        if ofs:
            return ofs, "OFS"
        return rows, None

    def company(self, corp_code: str) -> dict:
        return self._get_json(
            "company.json",
            {"corp_code": corp_code},
            f"company_{corp_code}",
        )

    def share_structure(self, corp_code: str, year: int, reprt_code: str) -> dict | None:
        data = self._get_json(
            "stockTotqySttus.json",
            {
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": reprt_code,
            },
            f"shares_{corp_code}_{year}_{reprt_code}",
        )
        rows = data.get("list") or []
        if not rows:
            return None

        common = None
        preferred = []
        total = None
        for r in rows:
            label = str(r.get("se") or "").strip()
            low = label.lower()
            issued = _to_number(r.get("istc_totqy"))
            if issued is None or issued <= 0:
                continue
            if "보통주" in low or "common" in low:
                common = issued
            elif "우선" in low or "preferred" in low:
                preferred.append({"label": label, "shares": issued})
            elif "합계" in low or low == "계" or "total" in low:
                total = issued
        return {"common_shares": common, "preferred_classes": preferred, "total_issued_shares": total}



class MarcapStore:
    """Point-in-time KRX universe and price source using FinanceData/marcap."""

    def __init__(self):
        self.root = CACHE_DIR / "marcap"
        self.root.mkdir(parents=True, exist_ok=True)
        self._frames: dict[int, pd.DataFrame] = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "lattice-stock-analyzer-backtest/0.1"})

    def year(self, year: int) -> pd.DataFrame:
        if year in self._frames:
            return self._frames[year]

        path = self.root / f"marcap-{year}.parquet"
        if not path.exists():
            url = MARCAP_URL.format(year=year)

            def call():
                r = self.session.get(url, timeout=(5, 90))
                r.raise_for_status()
                return r.content

            raw = _retry(call, attempts=4, base_sleep=2.0)
            path.write_bytes(raw)

        df = pd.read_parquet(path)
        if "Date" not in df.columns:
            if getattr(df.index, "name", None) == "Date":
                df = df.reset_index()
            else:
                df = df.reset_index()
                if "Date" not in df.columns and "index" in df.columns:
                    df = df.rename(columns={"index": "Date"})
        df["Date"] = pd.to_datetime(df["Date"]).dt.date
        df["Code"] = df["Code"].astype(str).str.zfill(6)
        self._frames[year] = df
        return df

    def snapshot_on_or_after(self, target: date) -> tuple[date, pd.DataFrame]:
        for year in (target.year, target.year + 1):
            df = self.year(year)
            part = df[df["Date"] >= target]
            if not part.empty:
                d = min(part["Date"])
                snap = part[part["Date"] == d].copy()
                snap = snap[snap["Market"].isin(["KOSPI", "KOSDAQ"])]
                return d, snap
        raise RuntimeError(f"No Marcap trading day on/after {target}")

    def first_price_on_or_after(self, ticker: str, target: date, days: int = 20) -> tuple[float, date] | None:
        end = target + timedelta(days=days)
        frames = []
        for year in range(target.year, end.year + 1):
            df = self.year(year)
            part = df[
                (df["Code"] == ticker)
                & (df["Date"] >= target)
                & (df["Date"] <= end)
            ]
            if not part.empty:
                frames.append(part)
        if not frames:
            return None
        x = pd.concat(frames).sort_values("Date").iloc[0]
        return float(x["Close"]), x["Date"]

    def last_price_on_or_before(self, ticker: str, start: date, target: date) -> tuple[float, date] | None:
        for year in range(target.year, start.year - 1, -1):
            df = self.year(year)
            part = df[
                (df["Code"] == ticker)
                & (df["Date"] >= start)
                & (df["Date"] <= target)
            ]
            if not part.empty:
                x = part.sort_values("Date").iloc[-1]
                return float(x["Close"]), x["Date"]
        return None


def _normalize_corp_name(name: str) -> str:
    s = str(name or "").strip()
    for token in ("주식회사", "(주)", "㈜"):
        s = s.replace(token, "")
    return "".join(s.split()).lower()

def filing_date(rows: list[dict] | None) -> date | None:
    if not rows:
        return None
    vals = []
    for r in rows:
        receipt = str(r.get("rcept_no") or "")
        if len(receipt) >= 8 and receipt[:8].isdigit():
            try:
                vals.append(datetime.strptime(receipt[:8], "%Y%m%d").date())
            except ValueError:
                pass
    return max(vals) if vals else None


def require_pit(rows: list[dict] | None, asof: date, label: str) -> None:
    fd = filing_date(rows)
    if fd is None:
        raise ValueError(f"{label}: receipt date missing")
    assert_point_in_time(fd, asof)


def first_trading_day_on_or_after(marcap: MarcapStore, year: int, month: int = 6, day: int = 1) -> date:
    d, _ = marcap.snapshot_on_or_after(date(year, month, day))
    return d


def market_cap_snapshot(marcap: MarcapStore, asof: date) -> tuple[dict[str, dict], dict[str, str]]:
    actual, frame = marcap.snapshot_on_or_after(asof)
    if actual != asof:
        raise RuntimeError(f"Marcap snapshot resolved to {actual}, expected {asof}")

    result = {}
    markets = {}
    for _, row in frame.iterrows():
        ticker = str(row["Code"]).zfill(6)
        cap = _to_number(row.get("Marcap"))
        shares = _to_number(row.get("Stocks"))
        close = _to_number(row.get("Close"))
        market = str(row.get("Market") or "")
        # FinanceData/marcap stores raw KRW market cap (not millions) in current parquet data.
        if cap is None or cap <= 0 or close is None or close <= 0:
            continue
        result[ticker] = {
            "ticker": ticker,
            "name": str(row.get("Name") or ""),
            "market_cap": cap,
            "listed_shares": shares,
            "close": close,
            "market": market,
        }
        markets[ticker] = market
    if not result:
        raise RuntimeError(f"Marcap market-cap snapshot is empty for {asof}")
    return result, markets


def build_corp_maps(corps: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    by_stock = {}
    by_name = {}
    for r in corps:
        if r.get("stock_code"):
            by_stock[r["stock_code"]] = r
        if r.get("corp_name"):
            by_name[_normalize_corp_name(r["corp_name"])] = r
    return by_stock, by_name


def annual_metrics(dart: DartCache, corp_code: str, report_year: int, asof: date) -> dict[int, dict]:
    full_rows, full_fs = dart.full(corp_code, report_year, "11011")
    major_rows, major_fs = dart.major(corp_code, report_year, "11011")
    require_pit(full_rows or major_rows, asof, f"FY{report_year}")

    out = {}
    for offset, amount_key in ((0, "thstrm_amount"), (1, "frmtrm_amount"), (2, "bfefrmtrm_amount")):
        year = report_year - offset
        base = (
            lfs._dart_metrics_from_rows(
                full_rows,
                income_key=amount_key,
                cash_key=amount_key,
                balance_key=amount_key,
            )
            if full_rows
            else {}
        )
        major = lfs._major_metrics_from_rows(major_rows, amount_key) if major_rows else {}
        m = lfs._merge_metrics(base, major)
        if m.get("revenue") is not None and m.get("operating_income") is not None:
            m["year"] = year
            m["_fs_div"] = major_fs or full_fs
            out[year] = m
    return out


def snapshot_score(
    dart: DartCache,
    corp: dict,
    ticker: str,
    asof: date,
    cap_snapshot: dict[str, dict],
) -> dict:
    score_year = asof.year
    latest_year = score_year - 1

    # Use enough annual reports to reconstruct the history that actually existed.
    report_years = [latest_year]
    history_year = max(2015, latest_year - 3)
    if history_year != latest_year:
        report_years.append(history_year)

    rows_by_year = {}
    filing_dates = []
    for ry in report_years:
        vals = annual_metrics(dart, corp["corp_code"], ry, asof)
        rows_by_year.update({y: m for y, m in vals.items() if y not in rows_by_year})
        full_rows, _ = dart.full(corp["corp_code"], ry, "11011")
        fd = filing_date(full_rows)
        if fd:
            filing_dates.append(fd.isoformat())

    if latest_year not in rows_by_year:
        raise ValueError("latest annual statement is unavailable point-in-time")

    rows = [rows_by_year[y] for y in sorted(rows_by_year)][-6:]
    if len(rows) < 2:
        raise ValueError("insufficient historical financial periods")

    annual_latest = rows_by_year[latest_year]

    # June ranking date: use Q1 only if it was actually filed by the ranking date.
    current = None
    try:
        q1_full, q1_fs = dart.full(corp["corp_code"], score_year, "11013")
        q1_major, q1_major_fs = dart.major(corp["corp_code"], score_year, "11013")
        prev_full, _ = dart.full(corp["corp_code"], score_year - 1, "11013")
        prev_major, _ = dart.major(corp["corp_code"], score_year - 1, "11013")
        if q1_full or q1_major:
            require_pit(q1_full or q1_major, asof, f"{score_year}Q1")
            # The prior-Q1 filing date is necessarily historical, but prove it too.
            require_pit(prev_full or prev_major, asof, f"{score_year-1}Q1")
            current = lfs._dart_interim_ttm(
                annual_latest,
                q1_full,
                score_year,
                "11013",
                q1_fs or q1_major_fs,
                major_rows=q1_major,
                prior_rows=prev_full,
                prior_major_rows=prev_major,
            )
            fd = filing_date(q1_full or q1_major)
            if fd:
                filing_dates.append(fd.isoformat())
    except Exception:
        # If current Q1 was not available on the exact historical date, use the
        # latest annual information that was available rather than future data.
        current = None

    cap_row = cap_snapshot.get(ticker)
    if not cap_row:
        raise ValueError("historical KRX market cap missing")

    share_structure = None
    try:
        share_structure = dart.share_structure(corp["corp_code"], score_year, "11013")
    except Exception:
        pass
    if not share_structure:
        try:
            share_structure = dart.share_structure(corp["corp_code"], latest_year, "11011")
        except Exception:
            pass

    equity_cap = cap_row["market_cap"]
    preferred_complete = True
    preferred_used = []

    pref_classes = (share_structure or {}).get("preferred_classes") or []
    if pref_classes:
        base = ticker[:-1] if len(ticker) == 6 else ""
        candidates = [base + x for x in ("5", "7", "9", "1")] if base else []
        found = [x for x in candidates if x in cap_snapshot]
        if len(found) < len(pref_classes):
            preferred_complete = False
            equity_cap = None
        else:
            for cls, pticker in zip(pref_classes, found):
                equity_cap += cap_snapshot[pticker]["market_cap"]
                preferred_used.append(pticker)

    balance = current or annual_latest
    ev = None
    if equity_cap is not None and balance.get("debt") is not None and balance.get("cash") is not None:
        ev = equity_cap + balance["debt"] - balance["cash"]

    info = {}
    try:
        info = dart.company(corp["corp_code"])
    except Exception:
        pass
    sector = lfs._sector_from_kr_industry(info.get("induty_code"))
    if sector == "Financials":
        raise ValueError("financial-sector company excluded: ROIC model is not comparable")

    result = lfs._compute_lfs(
        rows,
        tax_rate=0.24,
        current=current,
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
        "filing_dates_used": sorted(set(filing_dates)),
        "preferred_coverage_complete": preferred_complete,
        "preferred_tickers_used": preferred_used,
        "market_cap": equity_cap,
        "enterprise_value": ev,
    }


def start_price(marcap: MarcapStore, ticker: str, asof: date) -> tuple[float, date]:
    found = marcap.first_price_on_or_after(ticker, asof, days=20)
    if not found:
        raise ValueError("entry price unavailable")
    return found


def terminal_price(marcap: MarcapStore, ticker: str, start: date, target: date) -> tuple[float, date, str]:
    found = marcap.first_price_on_or_after(ticker, target, days=20)
    if found:
        px, dt = found
        return px, dt, "target_or_next_trading_day"

    # Delisted / suspended names are never silently removed.
    found = marcap.last_price_on_or_before(ticker, start, target)
    if not found:
        raise ValueError("no terminal or historical price")
    px, dt = found
    return px, dt, "last_available_before_target"


def benchmark_symbol(market: str) -> str:
    return "^KS11" if market == "KOSPI" else "^KQ11"


def yahoo_index_price(symbol: str, target: date, on_or_after: bool) -> tuple[float, date] | None:
    start = target - timedelta(days=10) if not on_or_after else target
    end = target + timedelta(days=15)
    params = {
        "period1": int(datetime.combine(start, datetime.min.time()).timestamp()),
        "period2": int(datetime.combine(end, datetime.min.time()).timestamp()),
        "interval": "1d",
        "events": "history",
    }

    def call():
        r = requests.get(
            YAHOO_CHART.format(symbol=symbol),
            params=params,
            headers={"User-Agent": "Mozilla/5.0 lattice-stock-analyzer-backtest"},
            timeout=(4, 15),
        )
        r.raise_for_status()
        return r.json()

    try:
        data = _retry(call, attempts=3)
        result = data["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        closes = result.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose")
        if not closes:
            closes = result.get("indicators", {}).get("quote", [{}])[0].get("close") or []
        pairs = []
        for ts, px in zip(timestamps, closes):
            if px is None:
                continue
            d = datetime.utcfromtimestamp(ts).date()
            pairs.append((d, float(px)))
        if not pairs:
            return None
        pairs.sort()
        if on_or_after:
            eligible = [x for x in pairs if x[0] >= target]
            return (eligible[0][1], eligible[0][0]) if eligible else None
        eligible = [x for x in pairs if x[0] <= target]
        return (eligible[-1][1], eligible[-1][0]) if eligible else None
    except Exception:
        return None


def completed_target(asof: date, years: int, today: date) -> date | None:
    try:
        target = asof.replace(year=asof.year + years)
    except ValueError:
        target = asof.replace(month=2, day=28, year=asof.year + years)
    return target if target <= today else None


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = []
    seen = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def run(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dart = DartCache(os.environ.get("DART_API_KEY", ""))
    marcap = MarcapStore()
    corps = dart.corp_codes()
    by_stock, by_name = build_corp_maps(corps)
    today = date.today()

    all_rank_rows = []
    observations: list[Observation] = []
    cohort_audits = []

    for year in range(args.start_year, args.end_year + 1):
        asof = first_trading_day_on_or_after(marcap, year)
        cap_snapshot, _ = market_cap_snapshot(marcap, asof)

        # Build the point-in-time universe as the largest DART-mappable
        # corporations, not simply the first N security codes. This prevents
        # ETFs, preferred shares and duplicate share classes from displacing
        # common-stock corporations from the research universe.
        securities = sorted(
            cap_snapshot.values(),
            key=lambda x: x["market_cap"],
            reverse=True,
        )

        mapped = []
        seen_corps = set()
        scanned_securities = 0
        for row in securities:
            scanned_securities += 1
            corp = by_stock.get(row["ticker"])
            if not corp:
                candidate = by_name.get(_normalize_corp_name(row.get("name")))
                if candidate:
                    current_common = candidate.get("stock_code")
                    # If the corporation's common code is present on the same
                    # historical date, a name-only alternate code is normally
                    # a preferred/other share class and must not be ranked.
                    if (
                        current_common
                        and current_common != row["ticker"]
                        and current_common in cap_snapshot
                    ):
                        candidate = None
                corp = candidate
            if not corp:
                continue
            if corp["corp_code"] in seen_corps:
                continue
            seen_corps.add(corp["corp_code"])
            mapped.append((row, corp))
            if len(mapped) >= args.universe_size:
                break

        mapping_coverage = (
            min(1.0, len(mapped) / args.universe_size)
            if args.universe_size > 0 else 0.0
        )
        scored = []
        errors = []
        for idx, (cap_row, corp) in enumerate(mapped, start=1):
            try:
                s = snapshot_score(dart, corp, cap_row["ticker"], asof, cap_snapshot)
                scored.append(s)
            except Exception as exc:
                errors.append(
                    {
                        "ticker": cap_row["ticker"],
                        "name": corp.get("corp_name"),
                        "error": str(exc),
                    }
                )
            if idx % 10 == 0:
                print(f"{year}: scored {idx}/{len(mapped)}; valid={len(scored)}")

        score_coverage = len(scored) / len(mapped) if mapped else 0.0
        ranked = rank_scores(scored, top_n=args.top_n)
        for row in ranked:
            row["cohort_year"] = year
            row["asof_date"] = asof.isoformat()
            all_rank_rows.append(row)

        cohort_audits.append(
            {
                "cohort_year": year,
                "asof_date": asof.isoformat(),
                "requested_corporations": args.universe_size,
                "scanned_securities": scanned_securities,
                "mapped_corporations": len(mapped),
                "scored_corporations": len(scored),
                "mapping_coverage": mapping_coverage,
                "score_coverage": score_coverage,
                "errors_sample": errors[:20],
            }
        )

        if mapping_coverage < args.min_mapping_coverage:
            raise RuntimeError(
                f"{year} mapping coverage {mapping_coverage:.1%} < {args.min_mapping_coverage:.1%}"
            )
        if score_coverage < args.min_score_coverage:
            raise RuntimeError(
                f"{year} score coverage {score_coverage:.1%} < {args.min_score_coverage:.1%}"
            )
        if len(ranked) < args.top_n:
            raise RuntimeError(f"{year} has only {len(ranked)} ranked names")

        benchmark_starts = {}
        for row in ranked:
            ticker = row["ticker"]
            entry, entry_date = start_price(marcap, ticker, asof)
            bsym = benchmark_symbol(row["market"])
            if bsym not in benchmark_starts:
                b_start = yahoo_index_price(bsym, entry_date, True)
                benchmark_starts[bsym] = b_start[0] if b_start else None
            for horizon in HORIZONS:
                target = completed_target(entry_date, horizon, today)
                if target is None:
                    continue
                end_px, end_date, terminal_source = terminal_price(marcap, ticker, entry_date, target)
                b_end = yahoo_index_price(bsym, target, False)
                ret = end_px / entry - 1.0
                bret = (
                    b_end[0] / benchmark_starts[bsym] - 1.0
                    if b_end and benchmark_starts.get(bsym)
                    else None
                )
                observations.append(
                    Observation(
                        cohort_year=year,
                        asof_date=asof.isoformat(),
                        rank=row["rank"],
                        ticker=ticker,
                        name=row["name"],
                        score=row["score"],
                        horizon_years=horizon,
                        start_price=entry,
                        end_price=end_px,
                        price_return=ret,
                        benchmark_return=bret,
                        terminal_source=terminal_source,
                    )
                )

    summary = summarize_rank_observations(observations)

    obs_rows = [asdict(x) | {"excess_return": x.excess_return} for x in observations]
    write_csv(OUT_DIR / "ranked_cohorts.csv", all_rank_rows)
    write_csv(OUT_DIR / "forward_returns.csv", obs_rows)
    write_csv(OUT_DIR / "rank_summary.csv", summary["rank_summary"])
    write_csv(OUT_DIR / "horizon_summary.csv", summary["horizon_summary"])

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "methodology_version": "pit-kr-v0.2-marcap",
        "parameters": vars(args),
        "cohort_audits": cohort_audits,
        **summary,
    }
    _json_dump(OUT_DIR / "results.json", payload)

    print(json.dumps(payload["horizon_summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start-year", type=int, default=2016)
    p.add_argument("--end-year", type=int, default=2025)
    p.add_argument("--universe-size", type=int, default=100)
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--min-mapping-coverage", type=float, default=0.95)
    p.add_argument("--min-score-coverage", type=float, default=0.80)
    args = p.parse_args()
    run(args)
