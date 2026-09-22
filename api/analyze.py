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
    params_base = {
        "crtfc_key": _dart_key(),
        "corp_code": corp_code,
        "bsns_year": str(year),
        "reprt_code": reprt_code,
    }

    # Most listed companies publish consolidated statements. Try CFS first,
    # with retries, then fall back to OFS only if CFS is truly unavailable.
    for fs_div in ("CFS", "OFS"):
        for timeout in ((2, 7), (3, 12)):
            try:
                data = _http_get(
                    f"{DART_BASE}/fnlttSinglAcntAll.json",
                    params={**params_base, "fs_div": fs_div},
                    timeout=timeout,
                ).json()
                if data.get("status") in (None, "000") and data.get("list"):
                    return data["list"], fs_div
                if data.get("status") == "013":
                    break
            except Exception:
                continue
    return None, None


def _dart_annual_rows(corp_code, year):
    return _dart_statement_rows(corp_code, year, "11011")


def _dart_company_info(corp_code):
    params = {"crtfc_key": _dart_key(), "corp_code": corp_code}
    for timeout in ((1.5, 4.5), (2, 7)):
        try:
            data = _http_get(
                f"{DART_BASE}/company.json",
                params=params,
                timeout=timeout,
            ).json()
            if data.get("status") in (None, "000"):
                return data
        except Exception:
            continue
    return {}



def _dart_major_rows(corp_code, year, reprt_code):
    params = {
        "crtfc_key": _dart_key(),
        "corp_code": corp_code,
        "bsns_year": str(year),
        "reprt_code": reprt_code,
    }
    for timeout in ((2, 6), (3, 10)):
        try:
            data = _http_get(
                f"{DART_BASE}/fnlttSinglAcnt.json",
                params=params,
                timeout=timeout,
            ).json()
            if data.get("status") in (None, "000") and data.get("list"):
                rows = data["list"]
                cfs = [r for r in rows if r.get("fs_div") == "CFS"]
                ofs = [r for r in rows if r.get("fs_div") == "OFS"]
                if cfs:
                    return cfs, "CFS"
                if ofs:
                    return ofs, "OFS"
                return rows, None
            if data.get("status") == "013":
                break
        except Exception:
            continue
    return None, None


def _major_value(rows, labels, amount_key):
    if not rows:
        return None
    labels_n = {str(x).replace(" ", "").lower() for x in labels}
    exact = []
    fuzzy = []
    for r in rows:
        nm = str(r.get("account_nm") or "").replace(" ", "").lower()
        v = _jnum(r.get(amount_key))
        if v is None:
            continue
        if nm in labels_n:
            exact.append(v)
        elif any(label in nm for label in labels_n):
            fuzzy.append(v)
    vals = exact or fuzzy
    if not vals:
        return None
    return max(vals, key=lambda x: abs(x))


def _major_metrics_from_rows(rows, amount_key):
    return {
        "revenue": _major_value(
            rows,
            ("매출액", "수익(매출액)", "영업수익", "매출"),
            amount_key,
        ),
        "operating_income": _major_value(
            rows,
            ("영업이익", "영업이익(손실)", "영업손익"),
            amount_key,
        ),
        "net_income": _major_value(
            rows,
            ("당기순이익", "당기순이익(손실)", "연결당기순이익", "반기순이익", "분기순이익"),
            amount_key,
        ),
        "assets": _major_value(rows, ("자산총계",), amount_key),
        "equity": _major_value(rows, ("자본총계",), amount_key),
    }


def _merge_metrics(base, override):
    out = dict(base or {})
    for k, v in (override or {}).items():
        if v is not None:
            out[k] = v
    return out

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


def _dart_metrics_from_rows(
    rows,
    income_key="thstrm_amount",
    cash_key=None,
    balance_key="thstrm_amount",
):
    # DART interim reports are asymmetric:
    # IS/CIS may expose quarter + cumulative columns (*_add_amount),
    # while CF is already cumulative and normally uses *_amount.
    cash_key = cash_key or income_key
    income_sj = ("IS", "CIS")

    # Revenue tags can coexist with extension/legacy tags. Prefer an exact
    # consolidated revenue label first; fall back to standardized IDs.
    revenue = None
    exact_revenue_names = {"매출액", "수익(매출액)", "영업수익"}
    revenue_candidates = []
    for r in rows:
        if not _sj_match(r.get("sj_div"), income_sj):
            continue
        nm = (r.get("account_nm") or "").replace(" ", "")
        if nm in exact_revenue_names:
            v = _jnum(r.get(income_key))
            if v is not None:
                revenue_candidates.append(v)
    if revenue_candidates:
        revenue = max(revenue_candidates, key=lambda x: abs(x))
    else:
        revenue = _row_value(
            rows,
            ids=("ifrs-full_RevenueFromContractsWithCustomers", "ifrs-full_Revenue"),
            names=("매출액", "영업수익", "수익(매출액)", "수익"),
            sj=income_sj,
            amount_key=income_key,
        )
    op_income = _row_value(
        rows,
        ids=("dart_OperatingIncomeLoss",),
        names=("영업이익", "영업이익(손실)", "영업손익"),
        sj=income_sj,
        amount_key=income_key,
    )
    net_income = _row_value(
        rows,
        ids=("ifrs-full_ProfitLoss",),
        names=("당기순이익", "당기순이익(손실)", "연결당기순이익", "분기순이익", "반기순이익"),
        sj=income_sj,
        amount_key=income_key,
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
        names=(
            "영업활동현금흐름",
            "영업활동으로인한현금흐름",
            "영업활동 현금흐름",
            "영업활동으로 인한 현금흐름",
            "영업활동에서창출된현금흐름",
            "영업활동에서 창출된 현금흐름",
        ),
        sj="CF",
        amount_key=cash_key,
    )
    capex = _row_value(
        rows,
        ids=("ifrs-full_PurchaseOfPropertyPlantAndEquipment",),
        names=(
            "유형자산의취득",
            "유형자산 취득",
            "유형자산취득",
            "유형자산의 취득",
            "유형자산의취득으로인한현금유출",
            "유형자산의 취득으로 인한 현금유출",
            "유형자산취득으로인한현금유출",
        ),
        sj="CF",
        amount_key=cash_key,
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


def _dart_interim_ttm(
    annual_latest,
    rows,
    year,
    reprt_code,
    fs_div,
    major_rows=None,
):
    if not annual_latest or annual_latest.get("year") != year - 1:
        return None
    if not rows and not major_rows:
        return None

    if reprt_code in ("11012", "11014"):
        current_income_key = "thstrm_add_amount"
        prior_income_key = "frmtrm_add_amount"
    else:
        current_income_key = "thstrm_amount"
        prior_income_key = "frmtrm_amount"

    # OpenDART full statements use cumulative CF values for interim reports.
    # Current CF is normally thstrm_amount; prior-year comparable cumulative
    # CF is normally frmtrm_add_amount. Fall back only when a field is absent.
    current_cash_key = "thstrm_amount"
    prior_cash_key = "frmtrm_add_amount"
    if rows:
        cf_rows = [r for r in rows if r.get("sj_div") == "CF"]
        if not any(_jnum(r.get(current_cash_key)) is not None for r in cf_rows):
            current_cash_key = "thstrm_add_amount"
        if not any(_jnum(r.get(prior_cash_key)) is not None for r in cf_rows):
            prior_cash_key = "frmtrm_amount"

    current_ytd = (
        _dart_metrics_from_rows(
            rows,
            income_key=current_income_key,
            cash_key=current_cash_key,
            balance_key="thstrm_amount",
        )
        if rows else {}
    )
    prior_ytd = (
        _dart_metrics_from_rows(
            rows,
            income_key=prior_income_key,
            cash_key=prior_cash_key,
            balance_key="frmtrm_amount",
        )
        if rows else {}
    )

    # The compact major-account endpoint is much less ambiguous for
    # revenue / operating profit / net income.
    if major_rows:
        current_major = _major_metrics_from_rows(major_rows, current_income_key)
        prior_major = _major_metrics_from_rows(major_rows, prior_income_key)
        current_ytd = _merge_metrics(current_ytd, current_major)
        prior_ytd = _merge_metrics(prior_ytd, prior_major)

    required = ("revenue", "operating_income", "net_income")
    if any(current_ytd.get(k) is None or prior_ytd.get(k) is None for k in required):
        return None

    flow_fields = ("revenue", "operating_income", "net_income", "cfo", "capex")
    out = {"year": year, "is_ttm": True, "fs_div": fs_div}

    for k in flow_fields:
        annual = annual_latest.get(k)
        cy = current_ytd.get(k)
        py = prior_ytd.get(k)
        out[k] = (
            annual + cy - py
            if annual is not None and cy is not None and py is not None
            else None
        )

    for k in ("assets", "equity", "cash", "debt"):
        out[k] = current_ytd.get(k) if current_ytd.get(k) is not None else annual_latest.get(k)

    label = {"11013": "Q1", "11012": "H1", "11014": "Q3"}.get(reprt_code, reprt_code)
    out["basis"] = f"TTM {year} {label} · FY{annual_latest['year']} bridge"
    out["report_code"] = reprt_code
    out["ttm_bridge"] = {
        "annual_year": annual_latest.get("year"),
        "annual": {k: annual_latest.get(k) for k in flow_fields},
        "current_ytd": {k: current_ytd.get(k) for k in flow_fields},
        "prior_ytd": {k: prior_ytd.get(k) for k in flow_fields},
    }
    return out


def _dart_share_count(corp_code, year, reprt_code="11011"):
    params = {
        "crtfc_key": _dart_key(),
        "corp_code": corp_code,
        "bsns_year": str(year),
        "reprt_code": reprt_code,
    }
    data = None
    for timeout in ((2, 6), (3, 10)):
        try:
            candidate = _http_get(
                f"{DART_BASE}/stockTotqySttus.json",
                params=params,
                timeout=timeout,
            ).json()
            if candidate.get("status") in (None, "000") and candidate.get("list"):
                data = candidate
                break
        except Exception:
            continue
    if not data:
        return None

    rows = data.get("list") or []

    # For the ordinary/common ticker, prefer the common issued-share row.
    for r in rows:
        se = str(r.get("se") or "").strip().lower()
        if "보통주" in se or "common" in se:
            v = _jnum(r.get("istc_totqy"))
            if v is not None and v > 0:
                return v
            v = _jnum(r.get("distb_stock_co"))
            if v is not None and v > 0:
                return v

    # If there is only one real security class, use its issued shares.
    class_rows = []
    for r in rows:
        se = str(r.get("se") or "").strip().lower()
        if any(x in se for x in ("합계", "total", "비고", "note")):
            continue
        v = _jnum(r.get("istc_totqy"))
        if v is not None and v > 0:
            class_rows.append(v)
    if len(class_rows) == 1:
        return class_rows[0]

    # Fallback to explicit total issued shares.
    for r in rows:
        se = str(r.get("se") or "").strip().lower()
        if "합계" in se or se == "계" or "total" in se:
            v = _jnum(r.get("istc_totqy"))
            if v is not None and v > 0:
                return v

    vals = [_jnum(r.get("istc_totqy")) for r in rows]
    vals = [v for v in vals if v is not None and v > 0]
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



def _sec_entries(cf, tags, units=("USD",)):
    facts = cf.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        block = facts.get(tag, {})
        unit_map = block.get("units", {})
        for unit in units:
            entries = unit_map.get(unit)
            if entries:
                return entries
    return []


def _date_days(start, end):
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
    except Exception:
        return None


def _sec_latest_ytd_pair(cf, tags):
    entries = _sec_entries(cf, tags)
    candidates = []
    for e in entries:
        if e.get("form") not in ("10-Q", "10-Q/A"):
            continue
        if e.get("fp") not in ("Q1", "Q2", "Q3"):
            continue
        if not e.get("start") or not e.get("end"):
            continue
        val = _jnum(e.get("val"))
        dur = _date_days(e.get("start"), e.get("end"))
        if val is None or dur is None or dur < 50 or dur > 310:
            continue
        candidates.append(
            {
                "val": val,
                "start": e.get("start"),
                "end": e.get("end"),
                "filed": e.get("filed", ""),
                "fp": e.get("fp"),
                "dur": dur,
            }
        )
    if not candidates:
        return None, None

    latest_end = max(x["end"] for x in candidates)
    same_end = [x for x in candidates if x["end"] == latest_end]
    current = max(same_end, key=lambda x: (x["dur"], x["filed"]))

    prior_candidates = []
    for x in candidates:
        if x["end"] >= current["end"] or x["fp"] != current["fp"]:
            continue
        try:
            gap = (datetime.fromisoformat(current["end"]) - datetime.fromisoformat(x["end"])).days
        except Exception:
            continue
        if 320 <= gap <= 410 and abs(x["dur"] - current["dur"]) <= 35:
            prior_candidates.append(x)
    prior = max(prior_candidates, key=lambda x: (x["end"], x["filed"])) if prior_candidates else None
    return current, prior


def _sec_latest_instant(cf, tags):
    entries = _sec_entries(cf, tags)
    candidates = []
    for e in entries:
        if e.get("form") not in ("10-Q", "10-Q/A", "10-K", "10-K/A"):
            continue
        val = _jnum(e.get("val"))
        end = e.get("end")
        if val is None or not end:
            continue
        candidates.append((end, e.get("filed", ""), val))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][2]


def _sec_ttm_row(cf, annual_latest):
    if not annual_latest:
        return None

    flow_tags = {
        "revenue": (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ),
        "operating_income": ("OperatingIncomeLoss",),
        "net_income": ("NetIncomeLoss", "ProfitLoss"),
        "cfo": ("NetCashProvidedByUsedInOperatingActivities",),
        "capex": (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
        ),
    }
    balance_tags = {
        "assets": ("Assets",),
        "equity": (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
        "cash": (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ),
    }

    pairs = {k: _sec_latest_ytd_pair(cf, tags) for k, tags in flow_tags.items()}
    rev_cur, rev_prev = pairs["revenue"]
    if not rev_cur or not rev_prev:
        return None

    out = {"year": int(rev_cur["end"][:4]), "is_ttm": True, "basis": f"TTM through {rev_cur['end']}"}
    for k in flow_tags:
        cur, prev = pairs[k]
        annual = annual_latest.get(k)
        if k == "capex":
            annual = abs(annual) if annual is not None else None
        if cur and prev and annual is not None:
            val = annual + cur["val"] - prev["val"]
            out[k] = abs(val) if k == "capex" else val
        else:
            out[k] = annual

    for k, tags in balance_tags.items():
        out[k] = _sec_latest_instant(cf, tags)
        if out[k] is None:
            out[k] = annual_latest.get(k)

    debt_parts = []
    for tag in ("LongTermDebtCurrent", "LongTermDebtNoncurrent", "ShortTermBorrowings"):
        v = _sec_latest_instant(cf, (tag,))
        if v is not None:
            debt_parts.append(v)
    if debt_parts:
        out["debt"] = sum(debt_parts)
    else:
        out["debt"] = annual_latest.get("debt")

    return out


def _sec_submissions(cik):
    url = f"{SEC_BASE}/submissions/CIK{cik:010d}.json"
    try:
        return _http_get(url, headers=_sec_headers(), timeout=(2, 5)).json()
    except Exception:
        return {}


def _sector_from_kr_industry(code):
    s = str(code or "").strip()
    p2 = s[:2] if len(s) >= 2 else ""
    if p2 in {"26", "27", "28", "58", "62", "63"}:
        return "Technology"
    if p2 in {"21", "86"}:
        return "Healthcare"
    if p2 in {"64", "65", "66"}:
        return "Financials"
    if p2 in {"35", "36"}:
        return "Utilities"
    if p2 in {"68"}:
        return "RealEstate"
    if p2 in {"19", "20", "23", "24"}:
        return "Materials"
    if p2 in {"05", "06", "07", "08", "09"}:
        return "Energy"
    if p2 in {"10", "11", "12", "13", "14", "15", "31", "45", "46", "47", "55", "56"}:
        return "Consumer"
    if p2 in {"49", "50", "51", "52", "53", "59", "60", "61"}:
        return "Communication"
    if p2 in {"25", "29", "30", "32", "33", "41", "42"}:
        return "Industrials"
    return "General"


def _sector_from_sic(sic):
    try:
        x = int(sic)
    except Exception:
        return "General"
    if 3570 <= x <= 3579 or 3670 <= x <= 3699 or 7370 <= x <= 7379:
        return "Technology"
    if 2830 <= x <= 2836 or 3840 <= x <= 3851 or 8000 <= x <= 8099:
        return "Healthcare"
    if 6000 <= x <= 6799:
        return "Financials"
    if 4900 <= x <= 4999:
        return "Utilities"
    if 6500 <= x <= 6559:
        return "RealEstate"
    if 1000 <= x <= 1499 or 2800 <= x <= 2899 or 3200 <= x <= 3499:
        return "Materials"
    if 1300 <= x <= 1389 or 2900 <= x <= 2999:
        return "Energy"
    if 2000 <= x <= 2399 or 2500 <= x <= 2599 or 5000 <= x <= 5999:
        return "Consumer"
    if 4800 <= x <= 4899 or 7800 <= x <= 7899:
        return "Communication"
    if 1500 <= x <= 1799 or 3500 <= x <= 3569 or 3700 <= x <= 3799 or 4000 <= x <= 4799:
        return "Industrials"
    return "General"


SECTOR_BENCHMARKS = {
    "Technology": {"roic": (0.12, 0.08), "margin": (0.15, 0.10), "growth": (0.08, 0.09), "fcf": (0.10, 0.08), "yield": (0.04, 0.025)},
    "Healthcare": {"roic": (0.09, 0.09), "margin": (0.12, 0.15), "growth": (0.08, 0.12), "fcf": (0.07, 0.10), "yield": (0.04, 0.03)},
    "Financials": {"roic": (0.07, 0.05), "margin": (0.15, 0.10), "growth": (0.05, 0.07), "fcf": (0.08, 0.08), "yield": (0.055, 0.03)},
    "Utilities": {"roic": (0.06, 0.04), "margin": (0.10, 0.06), "growth": (0.03, 0.04), "fcf": (0.04, 0.07), "yield": (0.055, 0.025)},
    "RealEstate": {"roic": (0.06, 0.05), "margin": (0.18, 0.12), "growth": (0.04, 0.07), "fcf": (0.08, 0.10), "yield": (0.06, 0.03)},
    "Materials": {"roic": (0.08, 0.06), "margin": (0.09, 0.07), "growth": (0.05, 0.08), "fcf": (0.06, 0.08), "yield": (0.055, 0.03)},
    "Energy": {"roic": (0.09, 0.08), "margin": (0.10, 0.08), "growth": (0.04, 0.12), "fcf": (0.08, 0.11), "yield": (0.06, 0.04)},
    "Consumer": {"roic": (0.10, 0.07), "margin": (0.10, 0.08), "growth": (0.06, 0.07), "fcf": (0.07, 0.07), "yield": (0.045, 0.025)},
    "Communication": {"roic": (0.09, 0.07), "margin": (0.14, 0.10), "growth": (0.06, 0.08), "fcf": (0.09, 0.08), "yield": (0.045, 0.03)},
    "Industrials": {"roic": (0.09, 0.06), "margin": (0.09, 0.06), "growth": (0.05, 0.06), "fcf": (0.06, 0.07), "yield": (0.05, 0.03)},
    "General": {"roic": (0.09, 0.07), "margin": (0.10, 0.08), "growth": (0.05, 0.08), "fcf": (0.07, 0.08), "yield": (0.05, 0.03)},
}


def _cdf_percentile(x, center, scale):
    if x is None or scale <= 0:
        return None
    z = (x - center) / scale
    return 100.0 * 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _industry_percentile(sector, roic, margin, growth, fcf_margin, yield_metric):
    b = SECTOR_BENCHMARKS.get(sector, SECTOR_BENCHMARKS["General"])
    values = []
    for key, x in (
        ("roic", roic),
        ("margin", margin),
        ("growth", growth),
        ("fcf", fcf_margin),
        ("yield", yield_metric),
    ):
        if x is None:
            continue
        center, scale = b[key]
        p = _cdf_percentile(x, center, scale)
        if p is not None:
            values.append(_clamp(p, 1, 99))
    return round(sum(values) / len(values), 1) if values else None


def _compute_lfs(series, tax_rate, price=None, shares=None, current=None, industry=None):
    clean = [
        dict(r) for r in sorted(series, key=lambda x: x["year"])
        if r.get("revenue") is not None and r.get("operating_income") is not None
    ][-6:]
    if len(clean) < 2:
        raise RuntimeError("LFS 계산에 필요한 연간 재무 데이터가 부족합니다.")

    def enrich(row, prev_ic=None):
        row["operating_margin"] = (
            row["operating_income"] / row["revenue"] if row.get("revenue") else None
        )
        row["nopat"] = (
            row["operating_income"] * (1 - tax_rate)
            if row.get("operating_income") is not None else None
        )
        if row.get("equity") is not None and row.get("debt") is not None and row.get("cash") is not None:
            row["invested_capital"] = row["equity"] + row["debt"] - row["cash"]
        elif row.get("assets") is not None and row.get("cash") is not None:
            row["invested_capital"] = row["assets"] - row["cash"]
        else:
            row["invested_capital"] = None
        row["fcf"] = (
            row["cfo"] - row["capex"]
            if row.get("cfo") is not None and row.get("capex") is not None else None
        )
        ic = row.get("invested_capital")
        avg_ic = None
        if ic is not None and prev_ic is not None and ic > 0 and prev_ic > 0:
            avg_ic = (ic + prev_ic) / 2
        elif ic is not None and ic > 0:
            avg_ic = ic
        row["roic_proxy"] = (
            row["nopat"] / avg_ic
            if row.get("nopat") is not None and avg_ic else None
        )
        return row

    prev_ic = None
    for r in clean:
        enrich(r, prev_ic)
        prev_ic = r.get("invested_capital")

    latest_annual = clean[-1]
    current_row = dict(current) if current else dict(latest_annual)
    enrich(current_row, latest_annual.get("invested_capital"))

    annual_roics = [r.get("roic_proxy") for r in clean if r.get("roic_proxy") is not None]
    median_roic = _median(annual_roics)
    current_roic = current_row.get("roic_proxy")

    rolling_iroics = []
    for i in range(3, len(clean)):
        a = clean[i - 3]
        b = clean[i]
        aic = a.get("invested_capital")
        bic = b.get("invested_capital")
        if aic is None or bic is None or not aic:
            continue
        delta_ic = bic - aic
        min_den = max(1.0, abs(aic) * 0.02)
        if abs(delta_ic) < min_den:
            continue
        delta_nopat = (b.get("nopat") or 0) - (a.get("nopat") or 0)
        rolling_iroics.append(delta_nopat / delta_ic)
    rolling_iroic = _median(rolling_iroics)

    first = clean[0]
    rev_cagr = _cagr(
        first.get("revenue"),
        latest_annual.get("revenue"),
        latest_annual["year"] - first["year"],
    )
    recent_rev_cagr = None
    if len(clean) >= 4:
        recent_rev_cagr = _cagr(
            clean[-4].get("revenue"),
            latest_annual.get("revenue"),
            latest_annual["year"] - clean[-4]["year"],
        )

    nopat_cagr = _cagr(
        first.get("nopat"),
        latest_annual.get("nopat"),
        latest_annual["year"] - first["year"],
    )

    annual_cash_conversions = []
    annual_fcf_margins = []
    positive_fcf_years = 0
    margins = []
    for r in clean:
        if r.get("operating_margin") is not None:
            margins.append(r["operating_margin"])
        if r.get("cfo") is not None and r.get("net_income") is not None and r["net_income"] > 0:
            annual_cash_conversions.append(r["cfo"] / r["net_income"])
        if r.get("fcf") is not None and r.get("revenue"):
            annual_fcf_margins.append(r["fcf"] / r["revenue"])
            if r["fcf"] > 0:
                positive_fcf_years += 1

    normalized_margin = _median(margins)
    margin_std = _stdev(margins)
    median_cash_conversion = _median(annual_cash_conversions)
    normalized_fcf_margin = _median(annual_fcf_margins)

    current_margin = current_row.get("operating_margin")
    current_cash_conversion = (
        current_row["cfo"] / current_row["net_income"]
        if current_row.get("cfo") is not None
        and current_row.get("net_income") is not None
        and current_row["net_income"] > 0 else None
    )
    current_fcf_margin = (
        current_row["fcf"] / current_row["revenue"]
        if current_row.get("fcf") is not None and current_row.get("revenue") else None
    )

    balance = current_row
    cash_assets = (
        balance["cash"] / balance["assets"]
        if balance.get("assets") and balance.get("cash") is not None else None
    )
    equity_assets = (
        balance["equity"] / balance["assets"]
        if balance.get("assets") and balance.get("equity") is not None else None
    )
    net_cash_assets = None
    if balance.get("assets") and balance.get("cash") is not None:
        debt = balance.get("debt") or 0
        net_cash_assets = (balance["cash"] - debt) / balance["assets"]

    margin_gap = (
        current_margin - normalized_margin
        if current_margin is not None and normalized_margin is not None else None
    )

    roic_persistent_years = sum(1 for x in annual_roics if x > 0.05)
    roic_persistence = (roic_persistent_years / max(1, len(annual_roics))) * 5
    margin_persistence = (
        1.5 if margin_std is None
        else _score_linear(0.15 - margin_std, 0.00, 0.15, 3)
    )
    gap_abs = abs(margin_gap) if margin_gap is not None else 0.10
    normalization_component = _score_linear(0.20 - gap_abs, 0.00, 0.20, 2)
    persistence_score = roic_persistence + margin_persistence + normalization_component

    financial = (
        _score_linear(net_cash_assets, -0.20, 0.20, 5)
        + _score_linear(equity_assets, 0.20, 0.70, 5)
    )

    growth_score = (
        _score_linear(rev_cagr, -0.05, 0.15, 8)
        + _score_linear(recent_rev_cagr, -0.05, 0.20, 4)
        + _score_linear(nopat_cagr, -0.10, 0.20, 3)
    )

    normalized_quality = (
        _score_linear(normalized_margin, 0.00, 0.25, 5)
        + _score_linear(median_roic, 0.00, 0.20, 5)
        + (2.5 if margin_std is None else _score_linear(0.15 - margin_std, 0.00, 0.15, 5))
    )
    current_quality = (
        _score_linear(current_margin, 0.00, 0.30, 5)
        + _score_linear(current_roic, 0.00, 0.25, 5)
        + (2.5 if margin_std is None else _score_linear(0.15 - margin_std, 0.00, 0.15, 5))
    )

    normalized_capital = (
        _score_linear(median_roic, 0.00, 0.20, 14)
        + _score_linear(rolling_iroic, 0.00, 0.30, 6)
    )
    current_capital = (
        _score_linear(current_roic, 0.00, 0.25, 14)
        + _score_linear(rolling_iroic, 0.00, 0.30, 6)
    )

    normalized_cash_quality = (
        _score_linear(median_cash_conversion, 0.50, 1.50, 6)
        + _score_linear(normalized_fcf_margin, -0.05, 0.20, 5)
        + (positive_fcf_years / max(1, len(clean))) * 4
    )

    # Do not manufacture a "neutral" current cash-flow score when the
    # latest-12-month CFO/FCF bridge is unavailable. Mark it missing and
    # reweight the remaining current-quality components instead.
    current_cash_data_available = (
        current_cash_conversion is not None and current_fcf_margin is not None
    )
    current_cash_quality = (
        _score_linear(current_cash_conversion, 0.50, 1.50, 6)
        + _score_linear(current_fcf_margin, -0.05, 0.25, 5)
        + (positive_fcf_years / max(1, len(clean))) * 4
        if current_cash_data_available
        else None
    )

    current_components = {
        "economic_quality_proxy": round(current_quality, 2),
        "capital_efficiency": round(current_capital, 2),
        "growth_reinvestment_proxy": round(growth_score, 2),
        "cash_quality": round(current_cash_quality, 2) if current_cash_quality is not None else None,
        "financial_strength": round(financial, 2),
        "persistence_normalization": round(persistence_score, 2),
    }
    normalized_components = {
        "economic_quality_proxy": round(normalized_quality, 2),
        "capital_efficiency": round(normalized_capital, 2),
        "growth_reinvestment_proxy": round(growth_score, 2),
        "cash_quality": round(normalized_cash_quality, 2),
        "financial_strength": round(financial, 2),
        "persistence_normalization": round(persistence_score, 2),
    }

    market_cap = price * shares if price and shares else None
    revenue_scale = current_row.get("revenue") or latest_annual.get("revenue")

    current_nopat_yield = (
        current_row.get("nopat") / market_cap
        if current_row.get("nopat") is not None and market_cap and market_cap > 0 else None
    )
    current_fcf_yield = (
        current_row.get("fcf") / market_cap
        if current_row.get("fcf") is not None and market_cap and market_cap > 0 else None
    )
    normalized_nopat = (
        revenue_scale * normalized_margin * (1 - tax_rate)
        if revenue_scale and normalized_margin is not None else None
    )
    normalized_fcf = (
        revenue_scale * normalized_fcf_margin
        if revenue_scale and normalized_fcf_margin is not None else None
    )
    normalized_nopat_yield = (
        normalized_nopat / market_cap
        if normalized_nopat is not None and market_cap and market_cap > 0 else None
    )
    normalized_fcf_yield = (
        normalized_fcf / market_cap
        if normalized_fcf is not None and market_cap and market_cap > 0 else None
    )

    valuation_available = market_cap is not None and market_cap > 0
    if valuation_available:
        current_valuation = (
            _score_linear(current_nopat_yield, 0.005, 0.09, 8)
            + _score_linear(current_fcf_yield, 0.00, 0.10, 7)
        )
        normalized_valuation = (
            _score_linear(normalized_nopat_yield, 0.005, 0.07, 8)
            + _score_linear(normalized_fcf_yield, 0.00, 0.08, 7)
        )
    else:
        current_valuation = None
        normalized_valuation = None

    component_max = {
        "economic_quality_proxy": 15.0,
        "capital_efficiency": 20.0,
        "growth_reinvestment_proxy": 15.0,
        "cash_quality": 15.0,
        "financial_strength": 10.0,
        "persistence_normalization": 10.0,
    }

    def _reweighted_quality(component_values):
        available_keys = [k for k, v in component_values.items() if v is not None]
        available_max = sum(component_max[k] for k in available_keys)
        available_score = sum(component_values[k] for k in available_keys)
        if available_max <= 0:
            return None, 0.0
        # Rescale the available quality evidence back to the 85-point quality scale.
        return round(available_score / available_max * 85.0, 1), available_max

    current_quality_score, current_quality_available_max = _reweighted_quality(current_components)
    normalized_quality_score, normalized_quality_available_max = _reweighted_quality(normalized_components)

    if current_quality_score is None or normalized_quality_score is None:
        raise RuntimeError("품질 점수 계산에 필요한 재무 데이터가 부족합니다.")

    if valuation_available:
        current_score = round(current_quality_score + current_valuation, 1)
        normalized_score = round(normalized_quality_score + normalized_valuation, 1)
        blended_score = round(0.4 * current_score + 0.6 * normalized_score, 1)
    else:
        # Keep a usable quality-only score but flag it as non-comparable to full LFS.
        current_score = round(current_quality_score / 85.0 * 100.0, 1)
        normalized_score = round(normalized_quality_score / 85.0 * 100.0, 1)
        blended_score = round(0.4 * current_score + 0.6 * normalized_score, 1)

    sector = (industry or {}).get("sector") or "General"
    industry_percentile_current = _industry_percentile(
        sector,
        current_roic,
        current_margin,
        recent_rev_cagr if recent_rev_cagr is not None else rev_cagr,
        current_fcf_margin,
        current_nopat_yield,
    )
    industry_percentile_normalized = _industry_percentile(
        sector,
        median_roic,
        normalized_margin,
        rev_cagr,
        normalized_fcf_margin,
        normalized_nopat_yield,
    )
    if industry_percentile_current is not None and industry_percentile_normalized is not None:
        industry_percentile = round(
            0.4 * industry_percentile_current + 0.6 * industry_percentile_normalized, 1
        )
    else:
        industry_percentile = industry_percentile_normalized or industry_percentile_current

    return {
        "score": blended_score,
        "current_score": current_score,
        "normalized_score": normalized_score,
        "current_quality_score": current_quality_score,
        "normalized_quality_score": normalized_quality_score,
        "valuation_available": valuation_available,
        "data_quality": {
            "current_cash_flow_available": current_cash_data_available,
            "current_quality_available_points": current_quality_available_max,
            "normalized_quality_available_points": normalized_quality_available_max,
            "warnings": (
                []
                if current_cash_data_available
                else ["최근 12개월 현금흐름 데이터가 없어 해당 항목을 제외하고 나머지 항목 비중을 재조정했습니다."]
            ),
        },
        "current_valuation_score": round(current_valuation, 1) if current_valuation is not None else None,
        "normalized_valuation_score": round(normalized_valuation, 1) if normalized_valuation is not None else None,
        "current_components": current_components,
        "normalized_components": normalized_components,
        "components": normalized_components,
        "industry_percentile": industry_percentile,
        "industry_percentile_current": industry_percentile_current,
        "industry_percentile_normalized": industry_percentile_normalized,
        "industry": industry or {"sector": "General"},
        "metrics": {
            "latest_year": latest_annual["year"],
            "basis": current_row.get("basis") or f"FY {latest_annual['year']}",
            "roic_proxy": current_roic,
            "median_roic_proxy": median_roic,
            "rolling_3y_incremental_roic_median": rolling_iroic,
            "rolling_3y_incremental_roic_observations": len(rolling_iroics),
            "revenue_cagr": rev_cagr,
            "recent_revenue_cagr": recent_rev_cagr,
            "nopat_cagr": nopat_cagr,
            "cash_conversion": current_cash_conversion,
            "median_cash_conversion": median_cash_conversion,
            "fcf_margin": current_fcf_margin,
            "normalized_fcf_margin": normalized_fcf_margin,
            "operating_margin": current_margin,
            "normalized_operating_margin": normalized_margin,
            "margin_gap": margin_gap,
            "cash_to_assets": cash_assets,
            "net_cash_to_assets": net_cash_assets,
            "equity_to_assets": equity_assets,
            "market_cap_approx": market_cap,
            "current_nopat_yield": current_nopat_yield,
            "current_fcf_yield": current_fcf_yield,
            "normalized_nopat_yield": normalized_nopat_yield,
            "normalized_fcf_yield": normalized_fcf_yield,
            "current_revenue": current_row.get("revenue"),
            "current_operating_income": current_row.get("operating_income"),
            "current_net_income": current_row.get("net_income"),
            "current_cfo": current_row.get("cfo"),
            "current_capex": current_row.get("capex"),
            "current_fcf": current_row.get("fcf"),
            "cash_flow_complete": (
                current_row.get("cfo") is not None and current_row.get("capex") is not None
            ),
        },
        "history": clean,
        "current_data": current_row,
        "methodology": {
            "version": "pilot-0.3.4",
            "main_score": "40% current + 60% normalized; quality-only rescaled when valuation data is unavailable",
            "industry_percentile_method": "sector-adjusted parametric benchmark; not yet a live peer cross-section",
            "note": "TTM이 가능하면 현재점수는 TTM을 사용하고, 정상화점수는 최근 연간 분포의 중앙값/지속성을 사용합니다. 업종 percentile은 무료 즉시조회 버전의 섹터 benchmark CDF입니다.",
        },
    }


def _kr_interim_target(now=None):
    now = now or datetime.utcnow()
    if now.month >= 11:
        return now.year, "11014", "Q3"
    if now.month >= 8:
        return now.year, "11012", "H1"
    if now.month >= 5:
        return now.year, "11013", "Q1"
    return None, None, None


def analyze_kr(q):
    company = _resolve_kr_company(q)
    now = datetime.utcnow()
    latest_year = now.year - 1
    history_year = latest_year - 3
    interim_year, interim_code, interim_label = _kr_interim_target(now)

    # Two annual reports are enough: each annual response contains current,
    # prior and two-years-prior comparative columns.
    with ThreadPoolExecutor(max_workers=7) as ex:
        latest_full_job = ex.submit(_dart_annual_rows, company["corp_code"], latest_year)
        history_full_job = ex.submit(_dart_annual_rows, company["corp_code"], history_year)
        latest_major_job = ex.submit(_dart_major_rows, company["corp_code"], latest_year, "11011")
        history_major_job = ex.submit(_dart_major_rows, company["corp_code"], history_year, "11011")
        interim_full_job = (
            ex.submit(_dart_statement_rows, company["corp_code"], interim_year, interim_code)
            if interim_code else None
        )
        interim_major_job = (
            ex.submit(_dart_major_rows, company["corp_code"], interim_year, interim_code)
            if interim_code else None
        )
        info_job = ex.submit(_dart_company_info, company["corp_code"])
        price_job = ex.submit(_kr_price, company["stock_code"]) if company["stock_code"] else None

        try:
            latest_full, latest_fs = latest_full_job.result(timeout=18)
        except Exception:
            latest_full, latest_fs = None, None
        try:
            history_full, history_fs = history_full_job.result(timeout=18)
        except Exception:
            history_full, history_fs = None, None
        try:
            latest_major, latest_major_fs = latest_major_job.result(timeout=14)
        except Exception:
            latest_major, latest_major_fs = None, None
        try:
            history_major, history_major_fs = history_major_job.result(timeout=14)
        except Exception:
            history_major, history_major_fs = None, None
        try:
            interim_rows, interim_fs = (
                interim_full_job.result(timeout=18) if interim_full_job else (None, None)
            )
        except Exception:
            interim_rows, interim_fs = None, None
        try:
            interim_major, interim_major_fs = (
                interim_major_job.result(timeout=14) if interim_major_job else (None, None)
            )
        except Exception:
            interim_major, interim_major_fs = None, None
        try:
            company_info = info_job.result(timeout=10) or {}
        except Exception:
            company_info = {}
        try:
            p = price_job.result(timeout=8) if price_job else None
        except Exception:
            p = None

    raw_sets = {
        latest_year: (latest_full, latest_fs, latest_major, latest_major_fs),
        history_year: (history_full, history_fs, history_major, history_major_fs),
    }

    rows_by_year = {}
    fs_used = {}

    for report_year in (latest_year, history_year):
        full_rows, fs_div, major_rows, major_fs = raw_sets[report_year]
        for offset, amount_key in (
            (0, "thstrm_amount"),
            (1, "frmtrm_amount"),
            (2, "bfefrmtrm_amount"),
        ):
            y = report_year - offset
            if y in rows_by_year:
                continue

            base = (
                _dart_metrics_from_rows(
                    full_rows,
                    income_key=amount_key,
                    cash_key=amount_key,
                    balance_key=amount_key,
                )
                if full_rows else {}
            )
            major = (
                _major_metrics_from_rows(major_rows, amount_key)
                if major_rows else {}
            )
            m = _merge_metrics(base, major)

            if m.get("revenue") is None or m.get("operating_income") is None:
                continue
            m["year"] = y
            rows_by_year[y] = m
            fs_used[y] = major_fs or fs_div

    if latest_year not in rows_by_year:
        raise RuntimeError(
            f"FY{latest_year} 사업보고서 핵심 재무값을 확인하지 못했습니다. "
            "오래된 연도로 TTM을 대체하지 않고 분석을 중단했습니다. 잠시 후 다시 시도해 주세요."
        )

    rows = [rows_by_year[y] for y in sorted(rows_by_year)][-6:]
    if len(rows) < 4:
        raise RuntimeError(
            "최근 장기 재무이력이 충분하지 않아 LFS를 안정적으로 계산할 수 없습니다."
        )

    annual_latest = rows_by_year[latest_year]
    current = None
    if interim_code and (interim_rows or interim_major):
        current = _dart_interim_ttm(
            annual_latest,
            interim_rows,
            interim_year,
            interim_code,
            interim_fs or interim_major_fs,
            major_rows=interim_major,
        )

    # Never label an old-FY bridge as current TTM.
    if interim_code and current is None:
        raise RuntimeError(
            f"{interim_year} {interim_label} 누적값으로 TTM을 검증하지 못했습니다. "
            "잘못된 TTM 점수 대신 분석을 중단했습니다."
        )

    # Share count is fetched after the financial bridge succeeds; a failed
    # valuation fetch must not corrupt quality calculations.
    shares = None
    share_year = interim_year if interim_code else latest_year
    share_code = interim_code if interim_code else "11011"
    try:
        shares = _dart_share_count(company["corp_code"], share_year, share_code)
    except Exception:
        shares = None
    if shares is None and share_year != latest_year:
        try:
            shares = _dart_share_count(company["corp_code"], latest_year, "11011")
        except Exception:
            shares = None

    price = p.get("price") if p else None
    industry_code = company_info.get("induty_code")
    sector = _sector_from_kr_industry(industry_code)
    industry = {
        "sector": sector,
        "industry_code": industry_code,
        "industry_name": sector,
        "market_class": company_info.get("corp_cls"),
    }

    result = _compute_lfs(
        rows,
        tax_rate=0.24,
        price=price,
        shares=shares,
        current=current,
        industry=industry,
    )
    result.update(
        {
            "market": "KR",
            "company": company["corp_name"],
            "ticker": company["stock_code"],
            "corp_code": company["corp_code"],
            "price": p,
            "shares_approx": shares,
            "annual_base_year": latest_year,
            "fs_div_by_year": fs_used,
            "interim_report": {
                "year": interim_year,
                "code": interim_code,
                "label": interim_label,
                "used_for_ttm": bool(current),
            },
            "sources": [
                "OpenDART full financial statements",
                "OpenDART major accounts",
                "Yahoo Finance chart endpoint (price fallback)",
            ],
        }
    )
    return result


def analyze_us(q):
    company = _sec_company(q)
    with ThreadPoolExecutor(max_workers=3) as ex:
        cf_job = ex.submit(_sec_companyfacts, company["cik"])
        sub_job = ex.submit(_sec_submissions, company["cik"])
        price_job = ex.submit(_yahoo_price, company["ticker"])
        cf = cf_job.result(timeout=10)
        try:
            submissions = sub_job.result(timeout=7)
        except Exception:
            submissions = {}
        try:
            p = price_job.result(timeout=6)
        except Exception:
            p = None

    rows = _sec_series(cf)
    rows = [r for r in rows if r["year"] >= datetime.utcnow().year - 9][-7:]
    if len(rows) < 2:
        raise RuntimeError("SEC Company Facts에서 충분한 연간 재무데이터를 찾지 못했습니다.")

    annual_latest = rows[-1]
    current = _sec_ttm_row(cf, annual_latest)
    shares = _sec_latest_shares(cf)
    price = p.get("price") if p else None

    sic = submissions.get("sic")
    sector = _sector_from_sic(sic)
    industry = {
        "sector": sector,
        "industry_code": str(sic) if sic is not None else None,
        "industry_name": submissions.get("sicDescription") or sector,
    }

    result = _compute_lfs(
        rows,
        tax_rate=0.21,
        price=price,
        shares=shares,
        current=current,
        industry=industry,
    )
    result.update(
        {
            "market": "US",
            "company": company["title"],
            "ticker": company["ticker"],
            "cik": company["cik"],
            "price": p,
            "shares_approx": shares,
            "sources": ["SEC EDGAR Company Facts", "SEC submissions", "Yahoo Finance chart endpoint (price fallback)"],
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
