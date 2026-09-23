from __future__ import annotations

import json
import os
import time
import zipfile
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import requests

DART_BASE = "https://opendart.fss.or.kr/api"


def _num(v: Any):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s or s in {"-", "--"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


class HistoricalDart:
    """Receipt-specific DART reader for point-in-time research.

    It never uses today's fnlttSinglAcnt(All) values as historical facts. A
    report is first resolved through list.json with end_de <= asof, then the
    exact receipt's XBRL ZIP is downloaded with fnlttXbrl.xml.
    """

    def __init__(self, api_key: str | None = None, cache_dir: str | Path | None = None):
        self.key = (api_key or os.getenv("DART_API_KEY") or "").strip()
        if not self.key:
            raise RuntimeError("DART_API_KEY is required")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "lattice-stock-analyzer-pit/0.2"})
        self.cache = Path(cache_dir or Path(__file__).resolve().parent / ".cache" / "pit_dart")
        self.cache.mkdir(parents=True, exist_ok=True)

    def _json(self, endpoint: str, params: dict, key: str):
        p = self.cache / f"{key}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        r = self.session.get(f"{DART_BASE}/{endpoint}", params={"crtfc_key": self.key, **params}, timeout=(5, 30))
        r.raise_for_status()
        data = r.json()
        if data.get("status") not in (None, "000", "013"):
            raise RuntimeError(f"DART {endpoint}: {data.get('status')} {data.get('message')}")
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        time.sleep(0.08)
        return data

    def annual_receipt(self, corp_code: str, business_year: int, asof: date) -> dict | None:
        # Annual reports are normally filed in the following calendar year.
        bgn = date(business_year + 1, 1, 1)
        if asof < bgn:
            return None
        data = self._json(
            "list.json",
            {
                "corp_code": corp_code,
                "bgn_de": bgn.strftime("%Y%m%d"),
                "end_de": asof.strftime("%Y%m%d"),
                "pblntf_ty": "A",
                "page_count": "100",
            },
            f"list_{corp_code}_{business_year}_{asof:%Y%m%d}",
        )
        rows = data.get("list") or []
        candidates = []
        for row in rows:
            nm = str(row.get("report_nm") or "")
            rno = str(row.get("rcept_no") or "")
            rdt = str(row.get("rcept_dt") or rno[:8])
            if "사업보고서" not in nm or "분기" in nm or "반기" in nm:
                continue
            # DART report names usually contain the fiscal period, e.g. (2015.12).
            if str(business_year) not in nm:
                continue
            if len(rdt) == 8 and rdt.isdigit() and rdt <= asof.strftime("%Y%m%d"):
                candidates.append((rdt, rno, row))
        if not candidates:
            return None
        # The latest receipt actually public by asof is the valid information set.
        candidates.sort(key=lambda x: (x[0], x[1]))
        return candidates[-1][2]

    def xbrl_zip(self, receipt_no: str) -> bytes:
        # Official API guide specifies the first 8 digits for fnlttXbrl.
        r8 = str(receipt_no)[:8]
        p = self.cache / f"xbrl_{receipt_no}.zip"
        if p.exists():
            return p.read_bytes()
        r = self.session.get(f"{DART_BASE}/fnlttXbrl.xml", params={"crtfc_key": self.key, "rcept_no": r8}, timeout=(5, 45))
        r.raise_for_status()
        raw = r.content
        if not raw.startswith(b"PK"):
            raise RuntimeError(f"XBRL unavailable for receipt {receipt_no}: {raw[:160]!r}")
        p.write_bytes(raw)
        time.sleep(0.08)
        return raw

    def xbrl_facts(self, receipt_no: str) -> list[dict]:
        raw = self.xbrl_zip(receipt_no)
        zf = zipfile.ZipFile(BytesIO(raw))
        instance_names = [n for n in zf.namelist() if n.lower().endswith((".xbrl", ".xml")) and not n.lower().endswith((".xsd",))]
        facts = []
        for name in instance_names:
            try:
                root = ET.fromstring(zf.read(name))
            except Exception:
                continue
            contexts = {}
            for e in root.iter():
                if e.tag.rsplit("}", 1)[-1] != "context":
                    continue
                cid = e.attrib.get("id")
                if not cid:
                    continue
                start = end = instant = None
                for x in e.iter():
                    local = x.tag.rsplit("}", 1)[-1]
                    if local == "startDate": start = (x.text or "").strip()
                    elif local == "endDate": end = (x.text or "").strip()
                    elif local == "instant": instant = (x.text or "").strip()
                contexts[cid] = {"start": start, "end": end, "instant": instant}
            for e in root.iter():
                cref = e.attrib.get("contextRef")
                if not cref or cref not in contexts:
                    continue
                val = _num(e.text)
                if val is None:
                    continue
                facts.append({
                    "tag": e.tag.rsplit("}", 1)[-1],
                    "value": val,
                    "context": cref,
                    **contexts[cref],
                    "unit": e.attrib.get("unitRef"),
                    "source_file": name,
                    "receipt_no": receipt_no,
                })
        return facts


def is_financial_company(industry_code: str | None, corp_name: str | None = None) -> bool:
    """Conservative financial-company gate for ROIC comparability.

    KSIC 64 includes holding companies, so 64/65/66 cannot be blanket-excluded.
    64992 (non-financial holding companies) is explicitly non-financial. Names
    are only a guardrail; the detailed industry code remains primary.
    """
    code = str(industry_code or "").strip()
    name = str(corp_name or "").replace(" ", "")
    if code.startswith("64992"):
        return False
    # Obvious non-financial group holding-company names seen in the large-cap universe.
    if any(x in name for x in ("SK", "LG", "CJ", "GS")) and ("지주" in name or name in {"SK", "LG", "CJ", "GS"}):
        return False
    if code.startswith("65") or code.startswith("66"):
        return True
    if code.startswith("64"):
        # 64x financial intermediation is financial except explicit non-financial holdings.
        return True
    return False
