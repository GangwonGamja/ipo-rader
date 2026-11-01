"""OpenDART 공식 공시 확인 (API 키 필요: DART_API_KEY, https://opendart.fss.or.kr 에서 무료 발급).

사용 API
- list.json           : 기간 내 발행공시(pblntf_ty=C) 목록 → 증권신고서(지분증권), 정정, 철회신고서 탐지
- estkRs.json         : 증권신고서 주요정보(지분증권) → 청약일·납입일·공모가액·공모주식수·자금사용목적
- company.json        : 기업개황 → 업종코드·대표자·설립일 (필요 시)
- fnlttSinglAcntAll   : 정기보고서 재무제표 (상장 전 기업은 대부분 없음 → 증권신고서 본문 수동입력 필요)
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import date, timedelta

from .. import config
from ..http import FetchError, fetch_json

API = "https://opendart.fss.or.kr/api/{ep}.json?{qs}"
VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"


class NotConfigured(Exception):
    pass


def _call(ep: str, **params):
    if not config.DART_API_KEY:
        raise NotConfigured("DART_API_KEY 미설정")
    params["crtfc_key"] = config.DART_API_KEY
    data = fetch_json(API.format(ep=ep, qs=urllib.parse.urlencode(params)), check_robots=False)
    st = data.get("status")
    if st == "013":  # 조회된 데이터 없음
        return {"list": []}
    if st in ("010", "011", "012", "901"):
        raise FetchError(f"DART 인증/권한 오류 {st}: {data.get('message')}", blocked=True)
    if st != "000":
        raise FetchError(f"DART 오류 {st}: {data.get('message')}")
    return data


# 기업을 지정하지 않은 list 조회는 검색 기간이 최대 3개월(DART 오류 100) → 89일로 제한
MAX_LOOKBACK_DAYS = 89
# IPO 후보: 비상장(E)·코넥스(N, 이전상장 공모). 유가(Y)·코스닥(K) 상장사의 유상증자 신고서는 제외
IPO_CORP_CLS = {"E", "N"}


def list_equity_filings(today: date, lookback_days: int = MAX_LOOKBACK_DAYS) -> list[dict]:
    """최근 증권신고서(지분증권)·정정·철회 공시 목록(상장사 유상증자 제외)."""
    lookback_days = min(lookback_days, MAX_LOOKBACK_DAYS)
    out, page = [], 1
    bgn = (today - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    while True:
        d = _call("list", bgn_de=bgn, end_de=end, pblntf_ty="C", page_no=page, page_count=100)
        for it in d.get("list", []):
            nm = it.get("report_nm", "")
            if "지분증권" not in nm and "철회" not in nm:
                continue
            if it.get("corp_cls") and it["corp_cls"] not in IPO_CORP_CLS:
                continue
            it["is_amendment"] = bool(re.search(r"\[(기재)?정정\]|정정", nm))
            it["is_withdrawal"] = "철회" in nm
            it["url"] = VIEWER.format(rcp=it.get("rcept_no"))
            out.append(it)
        if page >= int(d.get("total_page", 1) or 1):
            break
        page += 1
    return out


def _num(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _range(v):
    """'2026년 11월 02일 ~ 2026년 11월 03일' 등 → (start, end)"""
    ds = re.findall(r"(\d{4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})", v or "")
    if not ds:
        return None
    ds = [date(int(a), int(b), int(c)).isoformat() for a, b, c in ds]
    return {"start": ds[0], "end": ds[-1]}


def equity_offering(corp_code: str, today: date) -> dict:
    """증권신고서(지분증권) 주요정보를 표준 키로 변환. 값이 없는 항목은 넣지 않는다."""
    d = _call("estkRs", corp_code=corp_code,
              bgn_de=(today - timedelta(days=365)).strftime("%Y%m%d"), end_de=today.strftime("%Y%m%d"))
    facts: dict = {}
    groups = {g.get("title"): g.get("list", []) for g in d.get("group", [])} if "group" in d else {}
    general = (groups.get("일반사항") or [{}])[-1]
    if _range(general.get("sbd")):
        facts["sched.subscription"] = _range(general.get("sbd"))
    if _range(general.get("pymd")):
        facts["sched.payment"] = _range(general.get("pymd"))
    kinds = groups.get("증권의종류") or []
    # stkcnt = 증권수량(stksen은 '기명식보통주' 같은 증권의 종류명)
    total_shares = sum(_num(k.get("stkcnt")) or 0 for k in kinds) or None
    if total_shares:
        facts["offer.shares_total"] = total_shares
    prices = [_num(k.get("slprc")) for k in kinds if _num(k.get("slprc"))]
    if prices:
        facts["offer.price_filing"] = prices[-1]  # 신고서 기재 가액(확정 전엔 희망밴드 하단인 경우가 많음)
    seller = groups.get("매출인에관한사항") or []
    old = sum(_num(s.get("slstk")) or 0 for s in seller)
    if total_shares and seller:
        facts["offer.shares_old"] = old
        facts["offer.shares_new"] = total_shares - old
    use = groups.get("자금의사용목적") or []
    if use:
        facts["offer.use_of_proceeds"] = [{"purpose": u.get("se"), "amount": _num(u.get("amt"))} for u in use]
    uw = groups.get("인수인정보") or []
    if uw:
        names = {u.get("actnmn") or u.get("actsen") for u in uw}
        facts["_underwriters"] = sorted(n for n in names if n and not str(n).replace(",", "").isdigit())
        synd = {}
        for u in uw:  # 증권사별 인수 수량 → 청약 가능 증권사·배정 물량 참고
            nm, q = u.get("actnmn") or u.get("actsen"), _num(u.get("udtcnt"))
            if nm and q and not str(nm).replace(",", "").isdigit():
                synd[nm] = synd.get(nm, 0) + q
        if synd:
            facts["offer.syndicate"] = [{"firm": k, "shares": v} for k, v in sorted(synd.items(), key=lambda kv: -kv[1])]
    return facts


# ---------- 공시 본문(증권신고서·발행조건확정·투자설명서)에서 핵심 값 읽기 ----------

def document_text(rcept_no: str) -> str:
    """DART 공시 원문(ZIP 안의 XML)을 글자만 남겨 돌려준다."""
    import html as _html
    import io
    import zipfile
    from ..http import fetch
    if not config.DART_API_KEY:
        raise NotConfigured("DART_API_KEY 미설정")
    qs = urllib.parse.urlencode({"crtfc_key": config.DART_API_KEY, "rcept_no": rcept_no})
    body = fetch(f"https://opendart.fss.or.kr/api/document.xml?{qs}", check_robots=False, raw=True)
    if not body.startswith(b"PK"):  # 오류는 ZIP이 아니라 XML/JSON 메시지로 온다
        raise FetchError(f"DART 원문 응답이 ZIP이 아님: {body[:120]!r}")
    parts = []
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        for name in z.namelist():
            if not name.lower().endswith((".xml", ".html", ".htm")):
                continue
            raw = z.read(name)
            for enc in ("utf-8", "cp949", "euc-kr"):
                try:
                    parts.append(raw.decode(enc))
                    break
                except UnicodeDecodeError:
                    continue
    return clean_text(" ".join(parts))


def clean_text(xml: str) -> str:
    import html as _html
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", xml, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return " ".join(_html.unescape(t).replace("\u00a0", " ").split())


_D = r"(20\d{2})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})"
_WON = r"([1-9]\d{0,2}(?:,\d{3})+|[1-9]\d{3,6})\s*원"


def _date(m, i):
    try:
        return date(int(m.group(i)), int(m.group(i + 1)), int(m.group(i + 2))).isoformat()
    except ValueError:
        return None


def extract_doc_facts(text: str) -> dict:
    """공시 본문에서 이름표가 분명한 값만 읽는다. 이름표가 없으면 넣지 않는다(추측 금지)."""
    out: dict = {}
    m = re.search(r"(?:상장\s*예정\s*일|매매\s*개시\s*(?:예정\s*)?일)\s*(?:\(예정\))?\s*[:：]?\s*(?:은|는|:)?\s*" + _D, text)
    if m and _date(m, 1):
        out["sched.listing"] = {"start": _date(m, 1), "end": _date(m, 1)}
    m = re.search(r"확정\s*공모\s*가(?:액|격)?\s*[:：]?\s*(?:은|는)?\s*(?:1주당\s*)?(?:금\s*)?" + _WON, text) or \
        re.search(r"1주당\s*(?:확정\s*)?공모\s*가(?:액|격)?\s*(?:은|는)\s*" + _WON + r"\s*(?:으로|로)\s*(?:최종\s*)?확정", text)
    if m:
        out["offer.price_final"] = int(m.group(1).replace(",", ""))
    m = re.search(r"희망\s*공모\s*가(?:액|격)?\s*(?:밴드|범위|구간)?\s*[:：]?\s*(?:은|는)?\s*" + _WON + r"\s*[~∼～\-]\s*" + _WON, text)
    if m:
        lo, hi = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
        if 0 < lo <= hi:
            out["offer.price_band"] = {"low": lo, "high": hi}
    i = text.find("수요예측")
    while i != -1 and "demand.competition_ratio" not in out:
        win = text[i:i + 600]
        m = re.search(r"경쟁률\s*[:：]?\s*(?:은|는)?\s*(?:약\s*)?([\d,]+(?:\.\d+)?)\s*(?::|대)\s*1", win)
        if m:
            v = float(m.group(1).replace(",", ""))
            if 0 < v < 100000:
                out["demand.competition_ratio"] = v
        i = text.find("수요예측", i + 4)
    m = re.search(r"의무\s*보유\s*확약\s*(?:신청\s*)?비율\s*[:：]?\s*(?:은|는)?\s*(?:약\s*)?(\d{1,3}(?:\.\d+)?)\s*%", text)
    if m and float(m.group(1)) <= 100:
        out["demand.lockup_commit_ratio"] = float(m.group(1)) / 100
    m = re.search(r"최소\s*청약\s*(?:주식\s*)?(?:수량|수|단위)\s*[:：]?\s*(?:은|는)?\s*([\d,]+)\s*주", text)
    if m:
        out["offer.min_subscription"] = int(m.group(1).replace(",", ""))
    m = re.search(r"청약\s*증거금\s*(?:률|율|비율)?\s*[:：]?\s*(?:은|는)?\s*(?:청약\s*금액의\s*)?(\d{1,3})\s*%", text)
    if m and 0 < int(m.group(1)) <= 100:
        out["offer.deposit_rate"] = int(m.group(1)) / 100
    return out


def financials(corp_code: str, year: int, consolidated: bool = True) -> dict | None:
    """정기보고서 기반 연간 재무. 상장 전 기업은 보통 데이터가 없어 None."""
    try:
        d = _call("fnlttSinglAcntAll", corp_code=corp_code, bsns_year=year, reprt_code="11011",
                  fs_div="CFS" if consolidated else "OFS")
    except FetchError:
        return None
    rows = d.get("list", [])
    if not rows:
        return None
    pick = {
        "revenue": ("ifrs-full_Revenue", "매출액", "수익(매출액)", "영업수익"),
        "op_income": ("dart_OperatingIncomeLoss", "영업이익", "영업이익(손실)"),
        "net_income": ("ifrs-full_ProfitLoss", "당기순이익", "당기순이익(손실)"),
        "op_cf": ("ifrs-full_CashFlowsFromUsedInOperatingActivities", "영업활동현금흐름", "영업활동으로인한현금흐름"),
        "equity": ("ifrs-full_Equity", "자본총계"),
        "liabilities": ("ifrs-full_Liabilities", "부채총계"),
        "cash": ("ifrs-full_CashAndCashEquivalents", "현금및현금성자산"),
    }
    out = {}
    for k, ids in pick.items():
        for r in rows:
            if r.get("account_id") in ids or r.get("account_nm") in ids:
                v = _num(r.get("thstrm_amount"))
                if v is not None:
                    out[k] = v
                    break
    out["basis"] = "연결" if consolidated else "별도"
    return out
