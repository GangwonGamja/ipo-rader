"""KRX Open API (한국거래소, https://openapi.krx.co.kr) — 공식 시세·상장 정보.

KRX_AUTH_KEY 필요: 회원가입 → 서비스별 이용신청 → 거래소 승인 후 사용(하루 10,000회, 키 유효기간 1년).
요청: https://data-dbg.krx.co.kr/svc/apis/{경로}?basDd=YYYYMMDD , 헤더 AUTH_KEY. 응답 OutBlock_1. 자료는 전 영업일까지.
쓰는 서비스(이용신청 필요)
  주식  유가증권/코스닥 종목기본정보  sto/stk_isu_base_info, sto/ksq_isu_base_info  → 상장일(LIST_DD)·상장주식수
        유가증권/코스닥 일별매매정보  sto/stk_bydd_trd, sto/ksq_bydd_trd          → 시가·종가·시가총액
  지수  KOSPI/KOSDAQ 시리즈 일별시세 idx/kospi_dd_trd, idx/kosdaq_dd_trd          → 코스피·코스닥·코스피200
"""
from __future__ import annotations

from datetime import date, timedelta

from .. import config
from ..http import FetchError, fetch_json

BASE = "https://data-dbg.krx.co.kr/svc/apis/{path}?basDd={d}"
MARKETS = {"KOSPI": ("stk_isu_base_info", "stk_bydd_trd"), "KOSDAQ": ("ksq_isu_base_info", "ksq_bydd_trd")}


class NotConfigured(Exception):
    pass


def auth_key() -> str:
    """관리자 화면에서 등록한 키 → 서버 환경변수 순."""
    try:
        from ..macro import get_key
        return get_key("KRX_AUTH_KEY") or config.KRX_AUTH_KEY
    except Exception:  # noqa: BLE001
        return config.KRX_AUTH_KEY


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def code6(v: str | None) -> str | None:
    """단축코드(6자리) 또는 표준코드(ISIN, KR7xxxxxx..) → 6자리."""
    v = (v or "").strip()
    if len(v) == 6:
        return v
    if len(v) == 12 and v.startswith("KR"):
        return v[3:9]
    return v or None


def get(path: str, d: date) -> list[dict]:
    key = auth_key()
    if not key:
        raise NotConfigured("KRX_AUTH_KEY 미설정")
    data = fetch_json(BASE.format(path=path, d=d.strftime("%Y%m%d")), check_robots=False, headers={"AUTH_KEY": key})
    if not isinstance(data, dict) or "OutBlock_1" not in data:
        msg = data.get("respMsg") or data.get("message") or str(data)[:120] if isinstance(data, dict) else str(data)[:120]
        raise FetchError(f"KRX 응답 오류: {msg} (서비스 이용신청·승인 여부 확인)")
    return data.get("OutBlock_1") or []


def _walk_back(fn, as_of: date, days: int = 7):
    """휴장일이면 직전 영업일로 거슬러 조회."""
    for back in range(days + 1):
        d = as_of - timedelta(days=back)
        rows = fn(d)
        if rows:
            return d, rows
    return None, []


def base_info(as_of: date) -> dict[str, dict]:
    """6자리 코드 → {name, abbr, list_dd, market, shares}."""
    out = {}
    for mkt, (bi, _) in MARKETS.items():
        _, rows = _walk_back(lambda d: get(f"sto/{bi}", d), as_of)
        for r in rows:
            c = code6(r.get("ISU_SRT_CD") or r.get("ISU_CD"))
            ld = str(r.get("LIST_DD") or "").replace("/", "").replace("-", "")
            if not c:
                continue
            out[c] = {"code": c, "isin": r.get("ISU_CD"), "name": r.get("ISU_NM"), "abbr": r.get("ISU_ABBRV"), "market": mkt,
                      "list_dd": f"{ld[:4]}-{ld[4:6]}-{ld[6:8]}" if len(ld) == 8 else None,
                      "shares": _num(r.get("LIST_SHRS")), "kind": r.get("KIND_STKCERT_TP_NM")}
    return out


def daily(market: str, d: date) -> dict[str, dict]:
    """그날 시장 전 종목 시세: 6자리 코드 → {open, close, high, low, mktcap, shares}."""
    rows = get(f"sto/{MARKETS[market][1]}", d)
    return {code6(r.get("ISU_CD")): {"date": d.isoformat(), "name": r.get("ISU_NM"), "open": _num(r.get("TDD_OPNPRC")),
                                     "close": _num(r.get("TDD_CLSPRC")), "high": _num(r.get("TDD_HGPRC")),
                                     "low": _num(r.get("TDD_LWPRC")), "mktcap": _num(r.get("MKTCAP")),
                                     "shares": _num(r.get("LIST_SHRS"))} for r in rows if r.get("ISU_CD")}


INDEXES = {"코스피": ("idx/kospi_dd_trd", "kr.kospi"), "코스피 200": ("idx/kospi_dd_trd", "kr.kospi200"),
           "코스닥": ("idx/kosdaq_dd_trd", "kr.kosdaq")}


def index_day(d: date) -> dict[str, float]:
    """그날 코스피·코스피200·코스닥 종가: 시리즈 ID → 값(휴장일이면 빈 dict)."""
    out = {}
    for path in {p for p, _ in INDEXES.values()}:
        for r in get(path, d):
            nm = " ".join(str(r.get("IDX_NM") or "").split())
            if nm in INDEXES and INDEXES[nm][0] == path:
                v = _num(r.get("CLSPRC_IDX"))
                if v:
                    out[INDEXES[nm][1]] = v
    return out


def market_snapshot(as_of: date) -> dict[str, dict]:
    """종목코드 → {name, close, mktcap, shares, market} (비교기업 시가총액용)."""
    for back in range(8):
        d = as_of - timedelta(days=back)
        out = {}
        for mkt in MARKETS:
            for c, r in daily(mkt, d).items():
                out[c] = {"name": r["name"], "close": r["close"], "mktcap": r["mktcap"], "shares": r["shares"],
                          "market": mkt, "as_of": d.strftime("%Y%m%d")}
        if out:
            return out
    return {}
