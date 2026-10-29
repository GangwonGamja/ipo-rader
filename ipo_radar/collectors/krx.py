"""KRX Open API (https://openapi.krx.co.kr) — 비교기업 시가총액·상장주식수.

KRX_AUTH_KEY 필요(회원가입 후 서비스별 이용신청). 엔드포인트:
  유가증권 일별매매정보 /svc/apis/sto/stk_bydd_trd, 코스닥 /svc/apis/sto/ksq_bydd_trd (basDd=YYYYMMDD)
KIND(kind.krx.co.kr) 공모기업 현황은 공식 API가 없어 자동 수집하지 않는다(페이지 구조 확인 후 별도 승인 필요).
"""
from __future__ import annotations

from datetime import date, timedelta

from .. import config
from ..http import fetch_json

BASE = "https://data-dbg.krx.co.kr/svc/apis/sto/{ep}?basDd={d}"


class NotConfigured(Exception):
    pass


def market_snapshot(as_of: date) -> dict[str, dict]:
    """종목코드 → {name, close, mktcap, shares, market}. 휴장일이면 직전 영업일까지 최대 7일 거슬러 조회."""
    if not config.KRX_AUTH_KEY:
        raise NotConfigured("KRX_AUTH_KEY 미설정")
    for back in range(8):
        d = (as_of - timedelta(days=back)).strftime("%Y%m%d")
        out = {}
        for ep, mkt in (("stk_bydd_trd", "KOSPI"), ("ksq_bydd_trd", "KOSDAQ")):
            data = fetch_json(BASE.format(ep=ep, d=d), check_robots=False,
                              headers={"AUTH_KEY": config.KRX_AUTH_KEY})
            for r in data.get("OutBlock_1", []):
                out[r["ISU_CD"]] = {"name": r.get("ISU_NM"), "close": float(r["TDD_CLSPRC"].replace(",", "")),
                                    "mktcap": float(r["MKTCAP"].replace(",", "")),
                                    "shares": float(r["LIST_SHRS"].replace(",", "")), "market": mkt,
                                    "as_of": d}
        if out:
            return out
    return {}
