"""기업별 리서치 보고서 생성·버전 관리.

- 모든 수치 항목은 {값, 단위, 근거구분, 출처(링크·매체·유형), 자료 기준일, 수집 시각}을 함께 가진다.
- 근거구분: confirmed(확인된 실적·공시) / company_projection(회사 전망) / company_claim(회사 주장)
            / news_reported(보도 내용) / analyst_estimate(분석자 추정)
- 투자 포인트·위험은 규칙 기반으로만 만들고 각 항목에 근거 키를 붙인다. 근거가 부족하면 만들지 않는다.
- 내용(수집 시각 제외)이 바뀐 경우에만 새 버전을 저장 → 재실행해도 보고서가 늘어나지 않는다.
"""
from __future__ import annotations

from .db import dumps, iso, loads, sha
from .registry import SCHEDULE_KEYS, get_facts

BASIS_LABEL = {"confirmed": "확인된 실적·공시", "company_projection": "회사 전망", "company_claim": "회사 주장",
               "news_reported": "보도 내용", "analyst_estimate": "분석자 추정"}

REQUIRED = {
    "fin.annual": "최근 3개년 재무(매출·영업이익·순이익·영업현금흐름) — DART 증권신고서",
    "fin.quarter": "최신 분기 재무 — DART 증권신고서",
    "offer.price_band": "희망 공모가 범위 — 증권신고서",
    "offer.shares_post_ipo": "공모 후 총 주식수 — 증권신고서",
    "offer.shares_new": "신주 모집 주식수 — 증권신고서",
    "offer.use_of_proceeds": "자금 사용 목적 — 증권신고서",
    "offer.float_ratio": "상장 직후 유통가능물량 — 증권신고서",
    "offer.lockup": "보호예수·해제 일정 — 증권신고서",
    "sched.demand_forecast": "수요예측일 — 증권신고서/IPOSTOCK",
    "sched.subscription": "청약일 — 증권신고서/IPOSTOCK",
    "sched.refund": "환불일 — 증권신고서/IPOSTOCK",
    "sched.listing": "상장예정일 — KIND/IPOSTOCK",
    "offer.min_subscription": "최소 청약 주수(균등배정 기준) — 투자설명서",
    "offer.deposit_rate": "청약 증거금률 — 투자설명서",
    "offer.syndicate": "인수단(청약 가능 증권사) — 투자설명서",
    "profile.business_model": "사업모델 — 증권신고서 '사업의 내용'",
    "profile.competitors": "경쟁사 — 증권신고서",
    "profile.industry": "산업 전망 — 증권신고서",
}


def _item(facts, key, label):
    f = facts.get(key)
    if not f:
        return None
    return {"key": key, "label": label, "value": f["value"], "unit": f["unit"], "basis": f["basis"],
            "basis_label": BASIS_LABEL.get(f["basis"], f["basis"]), "source": f["source"],
            "as_of": f["as_of"], "collected_at": f["collected_at"]}


def _cagr(series: dict):
    ys = sorted(k for k, v in series.items() if v)
    if len(ys) < 2:
        return None
    a, b, n = series[ys[0]], series[ys[-1]], int(ys[-1]) - int(ys[0])
    return (b / a) ** (1 / n) - 1 if a > 0 and b > 0 and n > 0 else None


def build(conn, uid: str, *, stage: str, interest: dict | None, valuation: dict, supply: dict,
          company: dict, demand_info: dict | None = None) -> dict:
    facts = get_facts(conn, uid)
    I = lambda k, l: _item(facts, k, l)  # noqa: E731
    annual = (facts.get("fin.annual") or {}).get("value") or {}
    rev_series = {y: d.get("revenue") for y, d in annual.items()} if annual else \
        ((facts.get("fin.revenue_reported") or {}).get("value") or {})
    rev_key = "fin.annual" if annual else "fin.revenue_reported"

    ratios = {}
    if rev_series:
        ratios["revenue_cagr"] = _cagr(rev_series)
    if annual:
        y = max(annual)
        d = annual[y]
        if d.get("revenue"):
            if d.get("op_income") is not None:
                ratios["op_margin"] = d["op_income"] / d["revenue"]
            if d.get("net_income") is not None:
                ratios["net_margin"] = d["net_income"] / d["revenue"]
        if d.get("equity") and d.get("liabilities") is not None:
            ratios["debt_ratio"] = d["liabilities"] / d["equity"]
        ratios["cash"] = d.get("cash")
        ratios["year"] = y
    top_cust = (facts.get("fin.top_customer_share") or {}).get("value")

    points, risks = [], []
    if ratios.get("revenue_cagr") is not None and ratios["revenue_cagr"] >= 0.3:
        points.append({"text": f"매출 연평균 {ratios['revenue_cagr']:.0%} 성장", "evidence": [rev_key],
                       "basis": facts[rev_key]["basis"]})
    if ratios.get("op_margin") is not None and ratios["op_margin"] >= 0.15:
        points.append({"text": f"영업이익률 {ratios['op_margin']:.0%} (최근 연도)", "evidence": ["fin.annual"], "basis": "confirmed"})
    old = (facts.get("offer.shares_old") or {}).get("value")
    if old == 0:
        points.append({"text": "전량 신주 모집 — 공모자금 전액이 회사로 유입", "evidence": ["offer.shares_old"],
                       "basis": facts["offer.shares_old"]["basis"]})
    lc = supply.get("lockup_commit")
    if lc is not None and lc >= 0.3:
        points.append({"text": f"의무보유확약 {lc:.0%}", "evidence": ["demand.lockup_commit_ratio"], "basis": "confirmed"})
    if facts.get("profile.customers"):
        points.append({"text": f"주요 고객: {facts['profile.customers']['value']}", "evidence": ["profile.customers"],
                       "basis": facts["profile.customers"]["basis"]})
    if facts.get("note.market_share"):
        points.append({"text": facts["note.market_share"]["value"], "evidence": ["note.market_share"],
                       "basis": facts["note.market_share"]["basis"]})
    if valuation.get("verdict") == "상대적 저평가 가능성":
        points.append({"text": "확인 실적 기준 가치 대비 공모가 할인 폭이 큼(상대적 저평가 가능성)" if valuation.get("engine") == "underwriter_check" else "비교기업 대비 공모가가 보수 시나리오 가치보다 낮음", "evidence": ["valuation"], "basis": "analyst_estimate"})

    dm = demand_info or {}
    if dm.get("grade") == "강함":
        points.append({"text": f"기관 수요 강함 — 경쟁률 {dm['competition_ratio']:,.0f} 대 1(기관 수요 강도 {dm['score']:.0f}점)",
                       "evidence": ["demand.competition_ratio"], "basis": "news_reported"})
    at_low = (dm.get("components") or {}).get("price_position") is not None and dm["components"]["price_position"] <= 20
    if dm.get("grade") == "약함" or at_low:
        risks.append({"text": "확정 공모가가 희망밴드 하단 이하 — 기관 가격 수용도 낮음" if at_low
                      else f"기관 수요 약함(강도 {dm.get('score')}점)", "evidence": ["demand.competition_ratio"], "basis": "news_reported"})
    if annual and annual[max(annual)].get("op_income") is not None and annual[max(annual)]["op_income"] < 0:
        risks.append({"text": "최근 연도 영업적자", "evidence": ["fin.annual"], "basis": "confirmed"})
    if facts.get("note.risk_loss"):
        risks.append({"text": facts["note.risk_loss"]["value"], "evidence": ["note.risk_loss"], "basis": facts["note.risk_loss"]["basis"]})
    if facts.get("note.risk_capital"):
        risks.append({"text": facts["note.risk_capital"]["value"], "evidence": ["note.risk_capital"], "basis": facts["note.risk_capital"]["basis"]})
    if facts.get("note.risk_customers"):
        risks.append({"text": facts["note.risk_customers"]["value"], "evidence": ["note.risk_customers"],
                      "basis": facts["note.risk_customers"]["basis"]})
    if top_cust is not None and top_cust >= 0.5 and not facts.get("note.risk_customers"):
        risks.append({"text": f"최대 고객 매출 비중 {top_cust:.0%}", "evidence": ["fin.top_customer_share"], "basis": "confirmed"})
    for f in supply.get("flags", []):
        risks.append({"text": f, "evidence": ["offer.float_ratio", "offer.shares_old", "demand.lockup_commit_ratio"], "basis": "confirmed"})
    if ratios.get("debt_ratio") is not None and ratios["debt_ratio"] >= 2:
        risks.append({"text": f"부채비율 {ratios['debt_ratio']:.0%}", "evidence": ["fin.annual"], "basis": "confirmed"})
    fd = valuation.get("forecast_dependency")
    if fd is not None and fd >= 0.4:
        risks.append({"text": f"공모가 근거의 {fd:.0%}가 미래 추정 이익에 의존(확인 실적 기준 가치와 차이)", "evidence": ["valuation.underwriter"], "basis": "analyst_estimate"})
    if valuation.get("verdict") == "상대적 고평가 가능성":
        risks.append({"text": "확인 실적 기준 가치 대비 공모가 할인 폭이 작음(상대적 고평가 가능성)" if valuation.get("engine") == "underwriter_check" else "공모가가 비교기업 기준 낙관 시나리오 가치보다 높음", "evidence": ["valuation"], "basis": "analyst_estimate"})
    if company["status"] in ("postponed", "withdrawn"):
        risks.append({"text": "일정 연기" if company["status"] == "postponed" else "상장 철회", "evidence": ["status"], "basis": "news_reported"})
    conflicts = conn.execute("SELECT field FROM changes WHERE uid=? AND change_type='source_conflict'", (uid,)).fetchall()
    if conflicts:
        risks.append({"text": "출처 간 값 불일치(" + ", ".join(sorted({r['field'] for r in conflicts})) + ")",
                      "evidence": ["changes"], "basis": "analyst_estimate"})

    sched = {k: I(k, v) for k, v in SCHEDULE_KEYS.items() if facts.get(k)}
    band = (facts.get("offer.price_band") or {}).get("value")
    final = (facts.get("offer.price_final") or {}).get("value")
    shares_post = (facts.get("offer.shares_post_ipo") or {}).get("value")
    mcap = None
    if shares_post and (band or final):
        mcap = {"basis": "analyst_estimate", "formula": "공모가 × 공모 후 주식수",
                "low": (final or band["low"]) * shares_post, "high": (final or band["high"]) * shares_post}
    elif facts.get("offer.market_cap_band"):
        f = facts["offer.market_cap_band"]
        mcap = {"basis": f["basis"], "formula": "보도된 공모가 기준 예상 시가총액", "low": f["value"]["low"],
                "high": f["value"]["high"], "source": f["source"]}

    content = {
        "company": {k: company[k] for k in ("uid", "name", "market", "sector", "kind", "underwriters", "status",
                                            "verification", "is_sample")},
        "stage": stage,
        "summary": {"points": points[:3], "risks": risks[:3],
                    "points_note": None if len(points) >= 3 else ("확인된 근거 자료가 아직 없습니다 — 투자설명서 확인 필요" if not points else f"근거가 확인된 {len(points)}개만 표시"),
                    "risks_note": None if len(risks) >= 3 else ("확인된 위험 자료가 아직 없습니다 — 투자설명서 ‘투자위험요소’ 확인 필요" if not risks else f"근거가 확인된 {len(risks)}개만 표시")},
        "business": [x for x in (I("profile.business", "사업모델"), I("profile.products", "주요 제품"),
                                 I("profile.customers", "주요 고객"), I("profile.competitors", "경쟁사"),
                                 I("profile.industry", "산업 전망")) if x],
        "financials": [x for x in (I("fin.annual", "연간 재무"), I("fin.quarter", "최신 분기"),
                                   I("fin.revenue_reported", "보도된 매출"), I("fin.projection", "회사 제시 추정 실적"),
                                   I("fin.top_customer_share", "최대 고객 비중")) if x],
        "ratios": ratios,
        "offering": [x for x in (I("offer.price_band", "희망 공모가"), I("offer.price_final", "확정 공모가"),
                                 I("offer.shares_total", "공모주식수"), I("offer.shares_new", "신주 모집"),
                                 I("offer.shares_old", "구주 매출"), I("offer.proceeds_band", "공모금액"),
                                 I("offer.shares_post_ipo", "공모 후 주식수"),
                                 I("offer.use_of_proceeds", "자금 사용 목적"),
                                 I("offer.use_of_proceeds_text", "자금 사용 목적(보도)"),
                                 I("offer.min_subscription", "최소 청약 주수"), I("offer.deposit_rate", "청약 증거금률"),
                                 I("offer.syndicate", "인수단"),
                                 I("filing.registration", "증권신고서 제출")) if x],
        "market_cap_estimate": mcap,
        "schedule": sched,
        "supply": supply,
        "supply_items": [x for x in (I("offer.float_ratio", "유통가능물량 비율"), I("offer.lockup", "보호예수"),
                                     I("demand.competition_ratio", "수요예측 경쟁률"),
                                     I("demand.lockup_commit_ratio", "의무보유확약 비율"),
                                     I("subscription.competition_ratio", "청약 경쟁률")) if x],
        "valuation": valuation,
        "demand": demand_info,
        "interest": interest,
        "missing": [{"key": k, "need": v} for k, v in REQUIRED.items()
                    if k not in facts and not (k == "profile.business_model" and "profile.business" in facts)],
        "facts": facts,
        "notes": [x for x in (I("note.market_share", "참고"),) if x],
    }
    return content


def _strip_volatile(o):
    if isinstance(o, dict):
        return {k: _strip_volatile(v) for k, v in o.items() if k not in ("collected_at", "computed_at", "stats")}
    if isinstance(o, list):
        return [_strip_volatile(x) for x in o]
    return o


def _flat(o, prefix=""):
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            out.update(_flat(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(o, list) and o and isinstance(o[0], dict) and "key" in o[0]:
        for it in o:
            out[f"{prefix}[{it['key']}]"] = dumps(it.get("value"))
    else:
        out[prefix] = dumps(o)
    return out


DIFF_SECTIONS = ("schedule", "offering", "financials", "supply_items", "business", "valuation.verdict",
                 "valuation.scenarios", "summary.risks", "summary.points", "stage", "company.status")


def save(conn, uid: str, content: dict, run_id: str, at: str | None = None) -> tuple[int, bool, list]:
    """내용이 바뀐 경우에만 새 버전 저장. 반환: (버전, 새로저장여부, 변경목록)."""
    at = at or iso()
    stable = _strip_volatile(content)
    h = sha(dumps(stable), 32)
    last = conn.execute("SELECT * FROM reports WHERE uid=? ORDER BY version DESC LIMIT 1", (uid,)).fetchone()
    if last and last["content_hash"] == h:
        return last["version"], False, loads(last["diff"]) or []
    diff = []
    if last:
        old = _flat(_strip_volatile(loads(last["content"])))
        new = _flat(stable)
        for k in sorted(set(old) | set(new)):
            if not k.startswith(DIFF_SECTIONS) or old.get(k) == new.get(k):
                continue
            diff.append({"field": k, "before": loads(old[k]) if k in old else None,
                         "after": loads(new[k]) if k in new else None})
        old_risks = {r["text"] for r in loads(last["content"])["summary"]["risks"]}
        for r in content["summary"]["risks"]:
            if r["text"] not in old_risks:
                conn.execute(
                    "INSERT INTO changes(run_id,uid,change_type,field,before,after,detected_at) VALUES(?,?,?,?,?,?,?)",
                    (run_id, uid, "new_risk", "summary.risks", None, r["text"], at))
    ver = (last["version"] + 1) if last else 1
    content = dict(content, version=ver, generated_at=at, run_id=run_id)
    conn.execute("INSERT INTO reports(uid,version,content,content_hash,diff,run_id,created_at) VALUES(?,?,?,?,?,?,?)",
                 (uid, ver, dumps(content), h, dumps(diff), run_id, at))
    return ver, True, diff
