from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from backtest.pit_dart import HistoricalDart

# Ten non-financial large historical issuers used only as source-integrity
# spot checks. Financial companies are intentionally excluded because the LFS
# non-financial model excludes them and some 2015 financial-company filings do
# not expose an XBRL ZIP through fnlttXbrl.
TICKERS = ["005930", "005380", "000660", "005490", "012330", "000270", "015760", "017670", "035420", "051910"]


def main():
    d = HistoricalDart()
    corps = {x.get("stock_code"): x for x in _corp_codes(d)}
    out = []
    for ticker in TICKERS:
        corp = corps.get(ticker)
        if not corp:
            out.append({"ticker": ticker, "ok": False, "error": "corp code missing"})
            continue
        try:
            rec = d.annual_receipt(corp["corp_code"], 2015, date(2016, 6, 1))
            if not rec:
                raise RuntimeError("2015 annual receipt unavailable by 2016-06-01")
            facts = d.xbrl_facts(rec["rcept_no"])
            if not facts:
                raise RuntimeError("receipt-specific XBRL parsed zero numeric facts")
            rdt = str(rec.get("rcept_dt") or rec["rcept_no"][:8])
            if rdt > "20160601":
                raise RuntimeError(f"look-ahead receipt {rdt}")
            out.append({"ticker": ticker, "corp_name": corp["corp_name"], "ok": True, "rcept_no": rec["rcept_no"], "rcept_dt": rdt, "fact_count": len(facts), "sample_tags": sorted({f['tag'] for f in facts})[:20]})
        except Exception as exc:
            out.append({"ticker": ticker, "corp_name": corp.get("corp_name"), "ok": False, "error": str(exc)})
    p = Path("backtest/out/pit_xbrl_probe.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if sum(1 for x in out if x.get("ok")) < len(TICKERS):
        raise SystemExit(f"PIT XBRL probe failed: fewer than {len(TICKERS)}/{len(TICKERS)} source checks passed")


def _corp_codes(d):
    p = d.cache / "corp_codes_probe.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    import io, zipfile, xml.etree.ElementTree as ET
    r = d.session.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": d.key}, timeout=(5, 30))
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    rows=[]
    for item in root.findall("list"):
        rows.append({"corp_code":(item.findtext("corp_code") or "").strip(),"corp_name":(item.findtext("corp_name") or "").strip(),"stock_code":(item.findtext("stock_code") or "").strip()})
    p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


if __name__ == "__main__":
    main()
