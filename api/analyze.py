import json
import math
import os
import statistics
import zipfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET

import requests

DART_BASE = "https://opendart.fss.or.kr/api"
SEC_BASE = "https://data.sec.gov"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
DART_CORP_CACHE_URL = "https://raw.githubusercontent.com/jinhoo-choi/risk-news-crolling/main/dart_corp_codes.json"

_DART_CORPS = None
_SEC_TICKER_MAP = None


def _jnum(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s or s in {"-", "--", "N/A"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _score_linear(x, lo, hi, points):
    if x is None:
        return points / 2
    if hi == lo:
        return points / 2
    return _clamp((x - lo) / (hi - lo), 0, 1) * points


def _median(vals):
    vals = [x for x in vals if x is not None]
    return statistics.median(vals) if vals else None


def _stdev(vals):
    vals = [x for x in vals if x is not None]
    return statistics.pstdev(vals) if len(vals) >= 2 else None


def _cagr(start, end, periods):
    if start is None or end is None or periods <= 0 or start <= 0 or end <= 0:
        return None
    return (end / start) ** (1 / periods) - 1


def _http_get(url, **kwargs):
    headers = kwargs.pop("headers", {})
    timeout = kwargs.pop("timeout", (5, 10))
    headers.setdefault("User-Agent", "lattice-stock-analyzer/1.0")
    r = requests.get(url, headers=headers, timeout=timeout, **kwargs)
    r.raise_for_status()
    return r


def _dart_key():
    key = os.getenv("DART_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DART_API_KEY가 설정되지 않았습니다.")
    return key


def _load_dart_corps():
    global _DART_CORPS
    if _DART_CORPS is not None:
        return _DART_CORPS

    # 빠른 경로: 상장사 stock_code -> DART corp_code 공개 캐시.
    # 재무 숫자는 이 파일을 쓰지 않고, 식별자 해석에만 사용한다.
    try:
        data = _http_get(DART_CORP_CACHE_URL, timeout=(2, 4)).json()
        corps = []
        if isinstance(data, dict):
            for stock_code, item in data.items():
                corp_code = str((item or {}).get("corp_code", "")).strip()
                corp_name = str((item or {}).get("corp_name", "")).strip()
                stock_code = str(stock_code).strip()
                if corp_code and corp_name and stock_code:
                    corps.append(
                        {
                            "corp_code": corp_code,
                            "corp_name": corp_name,
                            "stock_code": stock_code,
                        }
                    )
        if corps:
            _DART_CORPS = corps
            return corps
    except Exception:
        pass

    # fallback: 공식 OpenDART 전체 고유번호 파일.
    r = _http_get(
        f"{DART_BASE}/corpCode.xml",
        params={"crtfc_key": _dart_key()},
        timeout=(2, 6),
    )
    try:
        zf = zipfile.ZipFile(BytesIO(r.content))
        xml_bytes = zf.read(zf.namelist()[0])
    except zipfile.BadZipFile:
        raise RuntimeError("OpenDART 고유번호 파일을 읽지 못했습니다. API 키를 확인하세요.")

    root = ET.fromstring(xml_bytes)
    corps = []
    for item in root.findall("list"):
        corp_code = (item.findtext("corp_code") or "").strip()
        corp_name = (item.findtext("corp_name") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        if corp_code and corp_name:
            corps.append(
                {"corp_code": corp_code, "corp_name": corp_name, "stock_code": stock_code}
            )
    _DART_CORPS = corps
    return corps


def _resolve_kr_company(q):
    q = q.strip()
    corps = _load_dart_corps()

    if q.isdigit() and len(q) == 6:
        for c in corps:
            if c["stock_code"] == q:
                return c

    exact = [c for c in corps if c["corp_name"].lower() == q.lower()]
    if exact:
        return exact[0]

    partial = [c for c in corps if q.lower() in c["corp_name"].lower() and c["stock_code"]]
    if len(partial) == 1:
        return partial[0]
    if partial:
        partial.sort(key=lambda x: (len(x["corp_name"]), x["corp_name"]))
        return partial[0]

    raise RuntimeError("OpenDART에서 해당 한국 종목을 찾지 못했습니다.")


def _dart_json(endpoint, params):
    p = {"crtfc_key": _dart_key(), **params}
    data = _http_get(
        f"{DART_BASE}/{endpoint}",
        params=p,
        timeout=(1.5, 3.5),
    ).json()
    if data.get("status") not in (None, "000"):
        return None
    return data


def _dart_statement_rows(corp_code, year, reprt_code):
    def fetch_one(fs_div):
        try:
            data = _dart_json(
                "fnlttSinglAcntAll.json",
                {
                    "corp_code": corp_code,
                    "bsns_year": str(year),
                    "reprt_code": reprt_code,
                    "fs_div": fs_div,
                },
            )
            if data and data.get("list"):
                return fs_div, data["list"]
        except Exception:
            pass
        return fs_div, None

    results = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        jobs = [ex.submit(fetch_one, "CFS"), ex.submit(fetch_one, "OFS")]
        for future in as_completed(jobs):
            fs_div, rows = future.result()
            if rows:
                results[fs_div] = rows

    if results.get("CFS"):
        return results["CFS"], "CFS"
    if results.get("OFS"):
        return results["OFS"], "OFS"
    return None, None


def _dart_annual_rows(corp_code, year):
    return _dart_statement_rows(corp_code, year, "11011")


def _dart_company_info(corp_code):
    try:
        return _dart_json("company.json", {"corp_code": corp_code}) or {}
    except Exception:
        return {}


def _sj_match(value, sj):
    if not sj:
        return True
    if isinstance(sj, (tuple, list, set)):
        return value in sj
    return value == sj


def _row_value(rows, ids=(), names=(), sj=None, amount_key="thstrm_amount"):
    for aid in ids:
        for r in rows:
            if not _sj_match(r.get("sj_div"), sj):
                continue
            if (r.get("account_id") or "") == aid:
                v = _jnum(r.get(amount_key))
                if v is not None:
                    return v

    names_l = [n.lower().replace(" ", "") for n in names]
    for r in rows:
        if not _sj_match(r.get("sj_div"), sj):
            continue
        nm = (r.get("account_nm") or "").lower().replace(" ", "")
        for n in names_l:
            if n in nm:
                v = _jnum(r.get(amount_key))
                if v is not None:
                    return v
    return None


def _first_existing_amount_key(rows, candidates):
    for key in candidates:
        for r in rows:
            if _jnum(r.get(key)) is not None:
                return key
    return candidates[-1]


def _dart_metrics_from_rows(rows, flow_key="thstrm_amount", balance_key="thstrm_amount"):
    income_sj = ("IS", "CIS")
    revenue = _row_value(
        rows,
        ids=("ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"),
        names=("매출액", "영업수익", "수익(매출액)", "수익"),
        sj=income_sj,
        amount_key=flow_key,
    )
    op_income = _row_value(
        rows,
        ids=("dart_OperatingIncomeLoss",),
        names=("영업이익", "영업이익(손실)", "영업손익"),
        sj=income_sj,
        amount_key=flow_key,
    )
    net_income = _row_value(
        rows,
        ids=("ifrs-full_ProfitLoss",),
        names=("당기순이익", "당기순이익(손실)", "연결당기순이익", "분기순이익", "반기순이익"),
        sj=income_sj,
        amount_key=flow_key,
    )
    assets = _row_value(
        rows,
        ids=("ifrs-full_Assets",),
        names=("자산총계",),
        sj="BS",
        amount_key=balance_key,
    )
    equity = _row_value(
        rows,
        ids=("ifrs-full_Equity",),
        names=("자본총계",),
        sj="BS",
        amount_key=balance_key,
    )
    cash = _row_value(
        rows,
        ids=("ifrs-full_CashAndCashEquivalents",),
        names=("현금및현금성자산",),
        sj="BS",
        amount_key=balance_key,
    )
    cfo = _row_value(
        rows,
        ids=("ifrs-full_CashFlowsFromUsedInOperatingActivities",),
        names=("영업활동현금흐름", "영업활동으로인한현금흐름"),
        sj="CF",
        amount_key=flow_key,
    )
    capex = _row_value(
        rows,
        ids=("ifrs-full_PurchaseOfPropertyPlantAndEquipment",),
        names=("유형자산의취득", "유형자산 취득", "유형자산취득"),
        sj="CF",
        amount_key=flow_key,
    )
    if capex is not None:
        capex = abs(capex)

    debt_ids = (
        "ifrs-full_ShorttermBorrowings",
        "ifrs-full_BorrowingsCurrent",
        "ifrs-full_CurrentPortionOfLongtermBorrowings",
        "ifrs-full_LongtermBorrowings",
        "ifrs-full_CurrentPortionOfBonds",
        "ifrs-full_BondsIssued",
    )
    debt_vals = []
    seen_ids = set()
    for r in rows:
        aid = r.get("account_id") or ""
        if r.get("sj_div") == "BS" and aid in debt_ids and aid not in seen_ids:
            v = _jnum(r.get(balance_key))
            if v is not None:
                debt_vals.append(v)
                seen_ids.add(aid)
    debt = sum(debt_vals) if debt_vals else None

    return {
        "revenue": revenue,
        "operating_income": op_income,
        "net_income": net_income,
        "assets": assets,
        "equity": equity,
        "cash": cash,
        "debt": debt,
        "cfo": cfo,
        "capex": capex,
    }


def _dart_interim_ttm(annual_latest, rows, year, reprt_code, fs_div):
    if not rows or not annual_latest:
        return None

    if reprt_code in ("11012", "11014"):
        cur_flow_key = _first_existing_amount_key(rows, ("thstrm_add_amount", "thstrm_amount"))
        prev_flow_key = _first_existing_amount_key(rows, ("frmtrm_add_amount", "frmtrm_amount"))
    else:
        cur_flow_key = "thstrm_amount"
        prev_flow_key = "frmtrm_amount"

    current_ytd = _dart_metrics_from_rows(rows, flow_key=cur_flow_key, balance_key="thstrm_amount")
    prior_ytd = _dart_metrics_from_rows(rows, flow_key=prev_flow_key, balance_key="frmtrm_amount")

    flow_fields = ("revenue", "operating_income", "net_income", "cfo", "capex")
    if current_ytd.get("revenue") is None or prior_ytd.get("revenue") is None:
        return None

    out = {"year": year, "is_ttm": True, "fs_div": fs_div}
    for k in flow_fields:
        a = annual_latest.get(k)
        cy = current_ytd.get(k)
        py = prior_ytd.get(k)
        out[k] = (a + cy - py) if a is not None and cy is not None and py is not None else a

    for k in ("assets", "equity", "cash", "debt"):
        out[k] = current_ytd.get(k) if current_ytd.get(k) is not None else annual_latest.get(k)

    label = {"11013": "Q1", "11012": "H1", "11014": "Q3"}.get(reprt_code, reprt_code)
    out["basis"] = f"TTM {year} {label}"
    out["report_code"] = reprt_code
    return out


def _dart_share_count(corp_code, year, reprt_code="11011"):
    data = _dart_json(
        "stockTotqySttus.json",
        {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt_code},
    )
    if not data or not data.get("list"):
        return None
    vals = []
    for r in data["list"]:
        v = _jnum(r.get("distb_stock_co"))
        if v is not None and v > 0:
            vals.append(v)
    return max(vals) if vals else None


def _yahoo_price(symbol):
    try:
        data = _http_get(
            YAHOO_CHART.format(symbol),
            params={"range": "5d", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0 lattice-stock-analyzer"},
            timeout=(2, 3),
        ).json()
        result = data.get("chart", {}).get("result", [None])[0]
        if not result:
            return None
        meta = result.get("meta", {})
        price = _jnum(meta.get("regularMarketPrice"))
        if price is None:
            closes = (result.get("indicators", {}).get("quote", [{}])[0].get("close") or [])
            closes = [x for x in closes if x is not None]
            price = float(closes[-1]) if closes else None
        return {"symbol": symbol, "price": price, "currency": meta.get("currency")}
    except Exception:
        return None


def _kr_price(stock_code):
    symbols = (stock_code + ".KS", stock_code + ".KQ")
    with ThreadPoolExecutor(max_workers=2) as ex:
        jobs = {ex.submit(_yahoo_price, s): s for s in symbols}
        found = []
        for future in as_completed(jobs):
            try:
                p = future.result()
            except Exception:
                p = None
            if p and p.get("price"):
                found.append(p)
        if found:
            found.sort(key=lambda x: 0 if str(x.get("symbol", "")).endswith(".KS") else 1)
            return found[0]
    return None


def _sec_headers():
    ua = os.getenv("SEC_USER_AGENT", "").strip() or "lattice-stock-analyzer/1.0"
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def _load_sec_tickers():
    global _SEC_TICKER_MAP
    if _SEC_TICKER_MAP is not None:
        return _SEC_TICKER_MAP
    data = _http_get(SEC_TICKERS, headers=_sec_headers()).json()
    mp = {}
    for _, item in data.items():
        ticker = str(item.get("ticker", "")).upper()
        if ticker:
            mp[ticker] = {
                "cik": int(item["cik_str"]),
                "title": item.get("title", ticker),
                "ticker": ticker,
            }
    _SEC_TICKER_MAP = mp
    return mp


def _sec_company(q):
    q = q.strip().upper()
    mp = _load_sec_tickers()
    if q in mp:
        return mp[q]
    matches = [v for v in mp.values() if q in v["title"].upper()]
    if matches:
        matches.sort(key=lambda x: (len(x["title"]), x["title"]))
        return matches[0]
    raise RuntimeError("SEC ticker 목록에서 해당 미국 종목을 찾지 못했습니다.")


def _sec_companyfacts(cik):
    url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik:010d}.json"
    return _http_get(url, headers=_sec_headers()).json()


def _sec_annual_series(cf, tags, units=("USD",)):
    facts = cf.get("facts", {}).get("us-gaap", {})
    selected = {}
    for tag in tags:
        block = facts.get(tag, {})
        unit_map = block.get("units", {})
        entries = None
        for unit in units:
            if unit in unit_map:
                entries = unit_map[unit]
                break
        if entries is None:
            continue

        for e in entries:
            if e.get("form") not in ("10-K", "10-K/A"):
                continue
            if e.get("fp") not in (None, "FY"):
                continue
            end = e.get("end")
            val = _jnum(e.get("val"))
            if not end or val is None:
                continue
            try:
                year = int(end[:4])
            except Exception:
                continue
            filed = e.get("filed", "")
            prev = selected.get(year)
            if prev is None or filed >= prev["filed"]:
                selected[year] = {"val": val, "filed": filed}
        if selected:
            break
    return {y: x["val"] for y, x in selected.items()}


def _sec_latest_shares(cf):
    facts = cf.get("facts", {})
    blocks = []
    if "dei" in facts:
        blocks.append(facts["dei"].get("EntityCommonStockSharesOutstanding", {}))
    if "us-gaap" in facts:
        blocks.append(facts["us-gaap"].get("CommonStockSharesOutstanding", {}))

    candidates = []
    for block in blocks:
        for unit, entries in block.get("units", {}).items():
            if unit.lower() not in ("shares", "share"):
                continue
            for e in entries:
                v = _jnum(e.get("val"))
                end = e.get("end") or ""
                filed = e.get("filed") or ""
                if v is not None:
                    candidates.append((end, filed, v))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][2]


def _sec_series(cf):
    tags = {
        "revenue": (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ),
        "operating_income": ("OperatingIncomeLoss",),
        "net_income": ("NetIncomeLoss", "ProfitLoss"),
        "assets": ("Assets",),
        "equity": (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
        "cash": (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ),
        "cfo": ("NetCashProvidedByUsedInOperatingActivities",),
        "capex": (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
        ),
    }
    debt_tags = (
        "LongTermDebtCurrent",
        "LongTermDebtNoncurrent",
        "ShortTermBorrowings",
        "LongTermDebt",
    )

    values = {k: _sec_annual_series(cf, v) for k, v in tags.items()}
    debt_components = [_sec_annual_series(cf, (t,)) for t in debt_tags]

    years = set()
    for d in values.values():
        years.update(d.keys())
    for d in debt_components:
        years.update(d.keys())

    rows = []
    for year in sorted(years):
        row = {"year": year}
        for k, d in values.items():
            row[k] = d.get(year)
        ds = [d.get(year) for d in debt_components if d.get(year) is not None]
        row["debt"] = sum(ds) if ds else None
        if row.get("capex") is not None:
            row["capex"] = abs(row["capex"])
        rows.append(row)
    return rows


def _compute_lfs(series, tax_rate, price=None, shares=None):
    clean = [
        r for r in sorted(series, key=lambda x: x["year"])
        if r.get("revenue") is not None and r.get("operating_income") is not None
    ][-5:]
    if len(clean) < 2:
        raise RuntimeError("LFS 계산에 필요한 연간 재무 데이터가 부족합니다.")

    # 먼저 손익/현금흐름/투하자본 원자료를 만든다.
    for r in clean:
        r["operating_margin"] = (
            r["operating_income"] / r["revenue"] if r.get("revenue") else None
        )
        r["nopat"] = (
            r["operating_income"] * (1 - tax_rate)
            if r.get("operating_income") is not None else None
        )
        if r.get("equity") is not None and r.get("debt") is not None and r.get("cash") is not None:
            r["invested_capital"] = r["equity"] + r["debt"] - r["cash"]
        elif r.get("assets") is not None and r.get("cash") is not None:
            r["invested_capital"] = r["assets"] - r["cash"]
        else:
            r["invested_capital"] = None
        r["fcf"] = (
            r["cfo"] - r["capex"]
            if r.get("cfo") is not None and r.get("capex") is not None else None
        )

    # ROIC는 기말 투하자본이 아니라 전기/당기 평균 투하자본을 우선 사용한다.
    for i, r in enumerate(clean):
        ic = r.get("invested_capital")
        prev_ic = clean[i - 1].get("invested_capital") if i > 0 else None
        avg_ic = None
        if ic is not None and prev_ic is not None and ic > 0 and prev_ic > 0:
            avg_ic = (ic + prev_ic) / 2
        elif ic is not None and ic > 0:
            avg_ic = ic
        r["roic_proxy"] = (
            r["nopat"] / avg_ic
            if r.get("nopat") is not None and avg_ic else None
        )

    latest = clean[-1]
    roics = [r.get("roic_proxy") for r in clean if r.get("roic_proxy") is not None]
    median_roic = _median(roics)
    roic = latest.get("roic_proxy")

    # 1년 증분은 경기민감주에서 지나치게 노이즈가 크므로 3년 증분을 우선한다.
    base_idx = -4 if len(clean) >= 4 else -2
    base = clean[base_idx]
    iroic = None
    if latest.get("invested_capital") is not None and base.get("invested_capital") is not None:
        delta_ic = latest["invested_capital"] - base["invested_capital"]
        delta_nopat = (latest.get("nopat") or 0) - (base.get("nopat") or 0)
        if abs(delta_ic) > 1:
            iroic = delta_nopat / delta_ic

    rev_cagr = _cagr(
        clean[0].get("revenue"),
        latest.get("revenue"),
        latest["year"] - clean[0]["year"],
    )
    recent_rev_cagr = None
    if len(clean) >= 4:
        recent_rev_cagr = _cagr(
            clean[-4].get("revenue"),
            latest.get("revenue"),
            latest["year"] - clean[-4]["year"],
        )

    nopat_cagr = _cagr(
        clean[0].get("nopat"),
        latest.get("nopat"),
        latest["year"] - clean[0]["year"],
    )

    cash_conversions = []
    fcf_margins = []
    positive_fcf_years = 0
    for r in clean:
        if r.get("cfo") is not None and r.get("net_income") is not None and r["net_income"] > 0:
            cash_conversions.append(r["cfo"] / r["net_income"])
        if r.get("fcf") is not None and r.get("revenue"):
            fcf_margins.append(r["fcf"] / r["revenue"])
            if r["fcf"] > 0:
                positive_fcf_years += 1

    cash_conversion = _median(cash_conversions)
    fcf_margin = _median(fcf_margins)

    margins = [r.get("operating_margin") for r in clean if r.get("operating_margin") is not None]
    normalized_margin = _median(margins)
    margin_std = _stdev(margins)
    margin_gap = (
        latest.get("operating_margin") - normalized_margin
        if latest.get("operating_margin") is not None and normalized_margin is not None
        else None
    )

    # 1) Economic quality proxy (15)
    # '해자'를 재무수치만으로 확정할 수 없으므로 경제적 질 proxy로 명시한다.
    quality_margin = _score_linear(normalized_margin, 0.00, 0.25, 5)
    quality_roic = _score_linear(median_roic, 0.00, 0.20, 5)
    quality_stability = (
        2.5 if margin_std is None
        else _score_linear(0.15 - margin_std, 0.00, 0.15, 5)
    )
    economic_quality = quality_margin + quality_roic + quality_stability

    # 2) Capital efficiency (20)
    capital = (
        _score_linear(median_roic, 0.00, 0.20, 10)
        + _score_linear(roic, 0.00, 0.20, 5)
        + _score_linear(iroic, 0.00, 0.30, 5)
    )

    # 3) Growth / reinvestment proxy (15)
    growth = (
        _score_linear(rev_cagr, -0.05, 0.15, 8)
        + _score_linear(recent_rev_cagr, -0.05, 0.20, 4)
        + _score_linear(nopat_cagr, -0.10, 0.20, 3)
    )

    # 4) Cash quality (15): 단일연도 대신 5년 중앙값 + FCF 지속성
    cash_quality = (
        _score_linear(cash_conversion, 0.50, 1.50, 6)
        + _score_linear(fcf_margin, -0.05, 0.20, 5)
        + (positive_fcf_years / max(1, len(clean))) * 4
    )

    # 5) Financial strength (10)
    cash_assets = (
        latest["cash"] / latest["assets"]
        if latest.get("assets") and latest.get("cash") is not None else None
    )
    equity_assets = (
        latest["equity"] / latest["assets"]
        if latest.get("assets") and latest.get("equity") is not None else None
    )
    net_cash_assets = None
    if latest.get("assets") and latest.get("cash") is not None:
        debt = latest.get("debt") or 0
        net_cash_assets = (latest["cash"] - debt) / latest["assets"]

    financial = (
        _score_linear(net_cash_assets, -0.20, 0.20, 5)
        + _score_linear(equity_assets, 0.20, 0.70, 5)
    )

    # 6) Persistence / normalization (10)
    # 단순 '영업이익 양수'가 아니라 ROIC 지속성과 마진 변동성을 함께 본다.
    roic_persistent_years = sum(1 for x in roics if x > 0.05)
    roic_persistence = (roic_persistent_years / max(1, len(roics))) * 5
    margin_persistence = (
        1.5 if margin_std is None
        else _score_linear(0.15 - margin_std, 0.00, 0.15, 3)
    )
    gap_abs = abs(margin_gap) if margin_gap is not None else 0.10
    normalization = _score_linear(0.20 - gap_abs, 0.00, 0.20, 2)
    persistence_score = roic_persistence + margin_persistence + normalization

    # 7) Valuation (15): 2% 미만을 즉시 0점 처리하는 절벽을 제거한다.
    market_cap = price * shares if price and shares else None
    normalized_nopat = (
        latest["revenue"] * normalized_margin * (1 - tax_rate)
        if latest.get("revenue") and normalized_margin is not None else None
    )
    normalized_yield = (
        normalized_nopat / market_cap
        if normalized_nopat is not None and market_cap and market_cap > 0
        else None
    )
    current_fcf_yield = (
        latest.get("fcf") / market_cap
        if latest.get("fcf") is not None and market_cap and market_cap > 0
        else None
    )
    valuation = (
        _score_linear(normalized_yield, 0.005, 0.065, 8)
        + _score_linear(current_fcf_yield, 0.00, 0.08, 7)
    )

    components = {
        "economic_quality_proxy": round(economic_quality, 2),
        "capital_efficiency": round(capital, 2),
        "growth_reinvestment_proxy": round(growth, 2),
        "cash_quality": round(cash_quality, 2),
        "financial_strength": round(financial, 2),
        "persistence_normalization": round(persistence_score, 2),
        "valuation": round(valuation, 2),
    }
    quality_score = round(sum(v for k, v in components.items() if k != "valuation"), 1)
    total = round(quality_score + components["valuation"], 1)

    return {
        "score": total,
        "quality_score": quality_score,
        "valuation_score": round(components["valuation"], 1),
        "components": components,
        "metrics": {
            "latest_year": latest["year"],
            "roic_proxy": roic,
            "median_roic_proxy": median_roic,
            "incremental_roic_proxy": iroic,
            "revenue_cagr": rev_cagr,
            "recent_revenue_cagr": recent_rev_cagr,
            "nopat_cagr": nopat_cagr,
            "cash_conversion": cash_conversion,
            "fcf_margin": fcf_margin,
            "operating_margin": latest.get("operating_margin"),
            "normalized_operating_margin": normalized_margin,
            "margin_gap": margin_gap,
            "cash_to_assets": cash_assets,
            "net_cash_to_assets": net_cash_assets,
            "equity_to_assets": equity_assets,
            "market_cap_approx": market_cap,
            "normalized_nopat_yield": normalized_yield,
            "current_fcf_yield": current_fcf_yield,
        },
        "history": clean,
        "methodology": {
            "version": "pilot-0.2",
            "note": "업종 percentile 전의 절대기준 파일럿입니다. 경제적 해자는 재무수치만으로 확정하지 않고 economic quality proxy로 표시합니다. 현재 점수는 최근 연간 공시 기준이며 TTM은 다음 단계에서 반영합니다.",
        },
    }


def analyze_kr(q):
    company = _resolve_kr_company(q)
    latest_year = datetime.utcnow().year - 1

    # 연간 공시 1건에는 당기/전기/전전기 값이 같이 들어온다.
    # 따라서 최신연도와 3년 전 공시만 가져오면 최대 6개 연도를 만들 수 있다.
    requested_years = [latest_year, latest_year - 3]
    raw_sets = {}

    with ThreadPoolExecutor(max_workers=2) as ex:
        jobs = {ex.submit(_dart_annual_rows, company["corp_code"], y): y for y in requested_years}
        for future in as_completed(jobs):
            y = jobs[future]
            try:
                raw, fs_div = future.result()
            except Exception:
                raw, fs_div = None, None
            if raw:
                raw_sets[y] = (raw, fs_div)

    rows_by_year = {}
    fs_used = {}

    for report_year, payload in raw_sets.items():
        raw, fs_div = payload
        for offset, amount_key in (
            (0, "thstrm_amount"),
            (1, "frmtrm_amount"),
            (2, "bfefrmtrm_amount"),
        ):
            y = report_year - offset
            if y in rows_by_year:
                continue
            m = _dart_metrics_from_rows(raw, amount_key=amount_key)
            # 핵심 손익 값이 둘 다 있어야 유효한 연도로 본다.
            if m.get("revenue") is None or m.get("operating_income") is None:
                continue
            m["year"] = y
            rows_by_year[y] = m
            fs_used[y] = fs_div

    rows = [rows_by_year[y] for y in sorted(rows_by_year)]
    rows = rows[-5:]

    if len(rows) < 2:
        # 드물게 비교열이 비어 있는 공시가 있으면 개별 연도 조회로 보완한다.
        fallback_years = list(range(latest_year, latest_year - 6, -1))
        for y in fallback_years:
            if y in rows_by_year:
                continue
            try:
                raw, fs_div = _dart_annual_rows(company["corp_code"], y)
            except Exception:
                continue
            if raw:
                m = _dart_metrics_from_rows(raw)
                if m.get("revenue") is not None and m.get("operating_income") is not None:
                    m["year"] = y
                    rows_by_year[y] = m
                    fs_used[y] = fs_div
            if len(rows_by_year) >= 5:
                break
        rows = [rows_by_year[y] for y in sorted(rows_by_year)][-5:]

    if len(rows) < 2:
        raise RuntimeError("OpenDART에서 LFS 계산에 필요한 최근 연간 재무제표를 충분히 찾지 못했습니다.")

    latest_data_year = rows[-1]["year"]

    # 주식수와 무료 가격 데이터는 병렬 조회한다.
    with ThreadPoolExecutor(max_workers=2) as ex:
        share_job = ex.submit(_dart_share_count, company["corp_code"], latest_data_year)
        price_job = ex.submit(_kr_price, company["stock_code"]) if company["stock_code"] else None
        try:
            shares = share_job.result(timeout=10)
        except Exception:
            shares = None
        try:
            p = price_job.result(timeout=8) if price_job else None
        except Exception:
            p = None

    price = p.get("price") if p else None

    result = _compute_lfs(rows, tax_rate=0.24, price=price, shares=shares)
    result.update(
        {
            "market": "KR",
            "company": company["corp_name"],
            "ticker": company["stock_code"],
            "corp_code": company["corp_code"],
            "price": p,
            "shares_approx": shares,
            "fs_div_by_year": fs_used,
            "sources": ["OpenDART", "Yahoo Finance chart endpoint (price fallback)"],
        }
    )
    return result


def analyze_us(q):
    company = _sec_company(q)
    cf = _sec_companyfacts(company["cik"])
    rows = _sec_series(cf)
    rows = [r for r in rows if r["year"] >= datetime.utcnow().year - 7]
    shares = _sec_latest_shares(cf)
    p = _yahoo_price(company["ticker"])
    price = p.get("price") if p else None

    result = _compute_lfs(rows, tax_rate=0.21, price=price, shares=shares)
    result.update(
        {
            "market": "US",
            "company": company["title"],
            "ticker": company["ticker"],
            "cik": company["cik"],
            "price": p,
            "shares_approx": shares,
            "sources": ["SEC EDGAR Company Facts", "Yahoo Finance chart endpoint (price fallback)"],
        }
    )
    return result


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            return self._send_index()

        if parsed.path != "/api/analyze":
            return self._send(404, {"ok": False, "error": "Not found"})

        try:
            qs = parse_qs(parsed.query)
            market = (qs.get("market", ["KR"])[0] or "KR").upper()
            q = (qs.get("q", [""])[0] or "").strip()
            if not q:
                raise RuntimeError("종목명 또는 티커를 입력하세요.")

            if market == "KR":
                result = analyze_kr(q)
            elif market == "US":
                result = analyze_us(q)
            else:
                raise RuntimeError("market은 KR 또는 US여야 합니다.")

            self._send(200, {"ok": True, "data": result})
        except Exception as e:
            self._send(400, {"ok": False, "error": str(e)})

    def _send_index(self):
        index_path = Path(__file__).resolve().parent.parent / "index.html"
        try:
            body = index_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._send(500, {"ok": False, "error": f"index.html을 읽지 못했습니다: {e}"})

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
