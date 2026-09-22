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

    r = _http_get(f"{DART_BASE}/corpCode.xml", params={"crtfc_key": _dart_key()})
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
    data = _http_get(f"{DART_BASE}/{endpoint}", params=p).json()
    if data.get("status") not in (None, "000"):
        return None
    return data


def _dart_annual_rows(corp_code, year):
    for fs_div in ("CFS", "OFS"):
        data = _dart_json(
            "fnlttSinglAcntAll.json",
            {
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": "11011",
                "fs_div": fs_div,
            },
        )
        if data and data.get("list"):
            return data["list"], fs_div
    return None, None


def _row_value(rows, ids=(), names=(), sj=None):
    # Prefer standardized account ids.
    for aid in ids:
        for r in rows:
            if sj and r.get("sj_div") != sj:
                continue
            if (r.get("account_id") or "") == aid:
                v = _jnum(r.get("thstrm_amount"))
                if v is not None:
                    return v

    names_l = [n.lower() for n in names]
    for r in rows:
        if sj and r.get("sj_div") != sj:
            continue
        nm = (r.get("account_nm") or "").lower().replace(" ", "")
        for n in names_l:
            if n.replace(" ", "") in nm:
                v = _jnum(r.get("thstrm_amount"))
                if v is not None:
                    return v
    return None


def _dart_metrics_from_rows(rows):
    revenue = _row_value(
        rows,
        ids=("ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"),
        names=("매출액", "영업수익", "수익(매출액)"),
        sj="IS",
    )
    op_income = _row_value(
        rows,
        ids=("dart_OperatingIncomeLoss",),
        names=("영업이익", "영업이익(손실)"),
        sj="IS",
    )
    net_income = _row_value(
        rows,
        ids=("ifrs-full_ProfitLoss",),
        names=("당기순이익", "당기순이익(손실)", "연결당기순이익"),
        sj="IS",
    )
    assets = _row_value(
        rows,
        ids=("ifrs-full_Assets",),
        names=("자산총계",),
        sj="BS",
    )
    equity = _row_value(
        rows,
        ids=("ifrs-full_Equity",),
        names=("자본총계",),
        sj="BS",
    )
    cash = _row_value(
        rows,
        ids=("ifrs-full_CashAndCashEquivalents",),
        names=("현금및현금성자산",),
        sj="BS",
    )
    cfo = _row_value(
        rows,
        ids=("ifrs-full_CashFlowsFromUsedInOperatingActivities",),
        names=("영업활동현금흐름", "영업활동으로인한현금흐름"),
        sj="CF",
    )
    capex = _row_value(
        rows,
        ids=("ifrs-full_PurchaseOfPropertyPlantAndEquipment",),
        names=("유형자산의취득", "유형자산 취득"),
        sj="CF",
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
            v = _jnum(r.get("thstrm_amount"))
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


def _dart_share_count(corp_code, year):
    data = _dart_json(
        "stockTotqySttus.json",
        {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": "11011"},
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
    for suffix in (".KS", ".KQ"):
        p = _yahoo_price(stock_code + suffix)
        if p and p.get("price"):
            return p
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
        r["roic_proxy"] = (
            r["nopat"] / r["invested_capital"]
            if r.get("nopat") is not None and r.get("invested_capital")
            and r["invested_capital"] > 0 else None
        )

    latest = clean[-1]
    prior = clean[-2]

    roic = latest.get("roic_proxy")
    delta_ic = None
    iroic = None
    if latest.get("invested_capital") is not None and prior.get("invested_capital") is not None:
        delta_ic = latest["invested_capital"] - prior["invested_capital"]
        delta_nopat = (latest.get("nopat") or 0) - (prior.get("nopat") or 0)
        if abs(delta_ic) > 1:
            iroic = delta_nopat / delta_ic

    rev_cagr = _cagr(
        clean[0].get("revenue"),
        latest.get("revenue"),
        latest["year"] - clean[0]["year"],
    )
    nopat_cagr = _cagr(
        clean[0].get("nopat"),
        latest.get("nopat"),
        latest["year"] - clean[0]["year"],
    )
    cash_conversion = (
        latest["cfo"] / latest["net_income"]
        if latest.get("cfo") is not None
        and latest.get("net_income") not in (None, 0)
        and latest["net_income"] > 0
        else None
    )
    fcf_margin = (
        latest["fcf"] / latest["revenue"]
        if latest.get("fcf") is not None and latest.get("revenue")
        else None
    )
    margins = [r.get("operating_margin") for r in clean if r.get("operating_margin") is not None]
    normalized_margin = _median(margins)
    margin_std = _stdev(margins)
    margin_gap = (
        latest.get("operating_margin") - normalized_margin
        if latest.get("operating_margin") is not None and normalized_margin is not None
        else None
    )

    # 1) Moat proxy: durable margin + low margin volatility.
    moat_margin = _score_linear(normalized_margin, 0.0, 0.30, 8)
    stability = 3.5 if margin_std is None else _score_linear(0.15 - margin_std, 0, 0.12, 7)
    moat = moat_margin + stability

    # 2) Capital efficiency.
    capital = _score_linear(roic, 0.0, 0.25, 14) + _score_linear(iroic, 0.0, 0.30, 6)

    # 3) Growth / reinvestment proxy.
    growth = _score_linear(rev_cagr, -0.05, 0.20, 8) + _score_linear(nopat_cagr, -0.05, 0.25, 7)

    # 4) Cash quality.
    cash_quality = _score_linear(cash_conversion, 0.5, 1.5, 7) + _score_linear(fcf_margin, -0.05, 0.20, 8)

    # 5) Balance-sheet resilience.
    if latest.get("assets") and latest.get("cash") is not None:
        cash_assets = latest["cash"] / latest["assets"]
    else:
        cash_assets = None
    if latest.get("assets") and latest.get("equity") is not None:
        equity_assets = latest["equity"] / latest["assets"]
    else:
        equity_assets = None
    financial = _score_linear(cash_assets, 0.02, 0.25, 5) + _score_linear(equity_assets, 0.20, 0.70, 5)

    # 6) Persistence / normalization.
    positive_years = sum(1 for m in margins if m > 0)
    persistence = (positive_years / max(1, len(margins))) * 5
    gap_abs = abs(margin_gap) if margin_gap is not None else 0.05
    normalization = _score_linear(0.25 - gap_abs, 0.0, 0.25, 5)
    persistence_score = persistence + normalization

    # 7) Valuation: normalized NOPAT yield on approximate market cap.
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
    valuation = _score_linear(normalized_yield, 0.02, 0.08, 15)

    components = {
        "moat_proxy": round(moat, 2),
        "capital_efficiency": round(capital, 2),
        "growth_reinvestment_proxy": round(growth, 2),
        "cash_quality": round(cash_quality, 2),
        "financial_strength": round(financial, 2),
        "persistence_normalization": round(persistence_score, 2),
        "valuation": round(valuation, 2),
    }
    total = round(sum(components.values()), 1)

    return {
        "score": total,
        "components": components,
        "metrics": {
            "latest_year": latest["year"],
            "roic_proxy": roic,
            "incremental_roic_proxy": iroic,
            "revenue_cagr": rev_cagr,
            "nopat_cagr": nopat_cagr,
            "cash_conversion": cash_conversion,
            "fcf_margin": fcf_margin,
            "operating_margin": latest.get("operating_margin"),
            "normalized_operating_margin": normalized_margin,
            "margin_gap": margin_gap,
            "cash_to_assets": cash_assets,
            "equity_to_assets": equity_assets,
            "market_cap_approx": market_cap,
            "normalized_nopat_yield": normalized_yield,
        },
        "history": clean,
        "methodology": {
            "version": "pilot-0.1",
            "note": "현재 버전은 업종 percentile 정규화 전의 파일럿 LFS입니다. ROIC는 공시 항목 가용성에 따라 capital-employed proxy를 사용할 수 있습니다.",
        },
    }


def analyze_kr(q):
    company = _resolve_kr_company(q)
    current_year = datetime.utcnow().year
    years = list(range(current_year - 1, current_year - 7, -1))
    rows = []
    fs_used = {}

    # DART 연도별 공시는 서로 독립이므로 병렬 조회해 Vercel 대기시간을 줄인다.
    with ThreadPoolExecutor(max_workers=6) as ex:
        jobs = {ex.submit(_dart_annual_rows, company["corp_code"], year): year for year in years}
        for future in as_completed(jobs):
            year = jobs[future]
            try:
                raw, fs_div = future.result()
            except Exception:
                continue
            if raw:
                m = _dart_metrics_from_rows(raw)
                m["year"] = year
                rows.append(m)
                fs_used[year] = fs_div

    rows.sort(key=lambda x: x["year"])
    rows = rows[-5:]
    if len(rows) < 2:
        raise RuntimeError("OpenDART에서 LFS 계산에 필요한 최근 연간 재무제표를 충분히 찾지 못했습니다.")

    latest_year = rows[-1]["year"]

    # 주식수와 무료 가격 데이터도 병렬 조회한다. 가격 실패는 분석 자체를 막지 않는다.
    with ThreadPoolExecutor(max_workers=2) as ex:
        share_job = ex.submit(_dart_share_count, company["corp_code"], latest_year)
        price_job = ex.submit(_kr_price, company["stock_code"]) if company["stock_code"] else None
        try:
            shares = share_job.result()
        except Exception:
            shares = None
        try:
            p = price_job.result() if price_job else None
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
