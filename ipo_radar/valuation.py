"""공모가의 상대적 적정성 평가 (설명 가능한 비교기업 배수법).

원칙
- 확인된 실적(confirmed)만으로 기본 가치를 계산한다. 회사 제시 미래실적은 별도 민감도 표로만 보여준다.
- 적자(조정 순이익 ≤ 0)면 PER을 쓰지 않는다. 비교기업의 음수·무의미 배수는 제외한다.
- 비교기업과 재무기간·연결/별도 기준이 다르면 제외하고 이유를 남긴다.
- 주당가치 = 지분가치 ÷ (공모 후 주식수 + 잠재 희석주식수).
- EV 계열은 공모 후 순차입금 = 차입금 − (현금 + 신주 공모대금) 으로 지분가치를 환산한다.
- 배수별 유효 비교기업이 3개 미만이면 그 배수는 쓰지 않는다. 쓸 수 있는 배수가 없으면 '판단 유보'.
- 시나리오: 보수=비교기업 배수 25백분위, 기준=중앙값, 낙관=75백분위.
"""
from __future__ import annotations

import statistics

from . import config

VERDICTS = ("상대적 저평가 가능성", "적정 범위", "상대적 고평가 가능성", "판단 유보")
FIN_SECTORS = ("금융", "은행", "보험", "증권", "리츠", "캐피탈")


def _v(facts, key):
    f = facts.get(key)
    return f["value"] if f else None


def _q(vals, p):
    vals = sorted(vals)
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def evaluate(facts: dict, sector: str | None = None) -> dict:
    reasons, assumptions = [], []
    annual = _v(facts, "fin.annual") or {}
    basis = _v(facts, "fin.basis")
    peers = _v(facts, "valuation.peers") or []
    shares_post = _v(facts, "offer.shares_post_ipo")
    dilutive = _v(facts, "offer.dilutive_shares") or 0
    band = _v(facts, "offer.price_band")
    final = _v(facts, "offer.price_final")
    shares_new = _v(facts, "offer.shares_new")
    one_off = _v(facts, "fin.one_off") or {}

    if not annual:
        reasons.append("확인된 재무자료(fin.annual) 없음 — 증권신고서 재무 입력 필요")
    if not shares_post:
        reasons.append("공모 후 주식수 없음 — 증권신고서 확인 필요")
    if not (band or final):
        reasons.append("희망 공모가·확정 공모가 없음")
    if not peers:
        reasons.append("비교기업 자료 없음 — 비교기업 선정·시세(KRX) 연결 필요")
    if reasons:
        return {"verdict": "판단 유보", "reasons": reasons, "methods": {}, "scenarios": None}

    year = max(annual)
    fy = annual[year]
    period = str(year)
    adj = one_off.get(year, {})
    ni = fy.get("net_income")
    ni_adj = (ni + adj.get("net_income_adj", 0)) if ni is not None else None
    if adj:
        assumptions.append(f"{year} 일회성 손익 조정: {adj.get('note', '')} ({adj.get('net_income_adj', 0):+,.0f}원)")
    price = final if final else (band["low"] + band["high"]) / 2
    price_label = "확정 공모가" if final else "희망밴드 중간값"
    proceeds_new = (shares_new or 0) * price
    if not shares_new:
        assumptions.append("신주 수 미확인 → 공모대금 유입 0으로 가정(보수적)")
    fee = _v(facts, "offer.fee_rate")
    if fee:
        proceeds_new *= (1 - fee)
        assumptions.append(f"공모 비용률 {fee:.1%} 차감")
    else:
        assumptions.append("공모 비용 미반영(총 공모대금 기준)")
    cash, debt = fy.get("cash"), fy.get("debt")
    net_debt_post = None
    if cash is not None and debt is not None:
        net_debt_post = debt - (cash + proceeds_new)
    shares_fd = shares_post + dilutive
    if dilutive:
        assumptions.append(f"잠재 희석주식 {dilutive:,.0f}주 반영(전환·행사 가정)")
    equity_post = (fy.get("equity") + proceeds_new) if fy.get("equity") is not None else None

    is_fin = any(s in (sector or "") for s in FIN_SECTORS)
    methods_spec = {
        "PER": ("net_income", ni_adj, False),
        "EV/EBITDA": ("ebitda", fy.get("ebitda"), True),
        "EV/Sales": ("revenue", fy.get("revenue"), True),
        "PBR": ("equity", equity_post, False),
    }
    methods, excluded_peers = {}, []
    for m, (metric, cval, is_ev) in methods_spec.items():
        why = None
        if m == "PER" and (cval is None or cval <= 0):
            why = "조정 순이익 ≤ 0 → PER 미적용"
        elif m == "PBR" and not is_fin:
            why = "금융·자산형 업종이 아니어서 PBR 미적용"
        elif m == "EV/Sales" and ni_adj is not None and ni_adj > 0 and fy.get("ebitda", 0) and fy["ebitda"] > 0:
            why = "흑자·EBITDA 양수 → 이익 기반 배수 우선, PSR 미적용"
        elif cval is None or cval <= 0:
            why = f"{metric} 값 없음 또는 ≤ 0"
        elif is_ev and net_debt_post is None:
            why = "현금·차입금 자료 없음 → EV 환산 불가"
        if why:
            methods[m] = {"used": False, "why": why}
            continue
        mults, used_peers = [], []
        for p in peers:
            if p.get("origin") != "analyst":
                continue
            if str(p.get("period")) != period:
                excluded_peers.append({"peer": p["name"], "method": m, "why": f"재무기간 불일치({p.get('period')}≠{period})"})
                continue
            if basis and p.get("fin_basis") and p["fin_basis"] != basis:
                excluded_peers.append({"peer": p["name"], "method": m, "why": f"연결/별도 불일치({p['fin_basis']}≠{basis})"})
                continue
            denom = p.get(metric if metric != "equity" else "equity")
            num = p.get("mktcap")
            if num is None or denom is None or denom <= 0:
                excluded_peers.append({"peer": p["name"], "method": m, "why": f"{metric} ≤ 0 또는 없음(음수 배수 제외)"})
                continue
            if is_ev:
                if p.get("net_debt") is None:
                    excluded_peers.append({"peer": p["name"], "method": m, "why": "순차입금 없음"})
                    continue
                num = num + p["net_debt"]
            mult = num / denom
            if mult <= 0 or mult > 200:
                excluded_peers.append({"peer": p["name"], "method": m, "why": f"비정상 배수 {mult:.1f}배"})
                continue
            mults.append(mult)
            used_peers.append({"name": p["name"], "multiple": round(mult, 2)})
        if len(mults) < config.MIN_VALID_PEERS:
            methods[m] = {"used": False, "why": f"유효 비교기업 {len(mults)}개 < {config.MIN_VALID_PEERS}개", "peers": used_peers}
            continue
        sc = {}
        for name, q in (("보수", 0.25), ("기준", 0.5), ("낙관", 0.75)):
            mult = _q(mults, q)
            eq = mult * cval - (net_debt_post if is_ev else 0)
            sc[name] = {"multiple": round(mult, 2), "equity_value": eq, "per_share": eq / shares_fd}
        methods[m] = {"used": True, "metric": metric, "company_metric": cval, "peers": used_peers, "scenarios": sc}

    used = {m: d for m, d in methods.items() if d["used"]}
    if not used:
        return {"verdict": "판단 유보", "reasons": ["적용 가능한 배수 없음"] + [f"{m}: {d['why']}" for m, d in methods.items()],
                "methods": methods, "excluded_peers": excluded_peers, "scenarios": None, "assumptions": assumptions}

    scenarios = {}
    for s in ("보수", "기준", "낙관"):
        v = statistics.mean(d["scenarios"][s]["per_share"] for d in used.values())
        scenarios[s] = {"per_share": round(v), "premium_vs_offer": price / v - 1 if v > 0 else None}
    lo, hi = sorted([scenarios["보수"]["per_share"], scenarios["낙관"]["per_share"]])
    if scenarios["기준"]["per_share"] <= 0:
        verdict = "판단 유보"
    elif price < lo:
        verdict = "상대적 저평가 가능성"
    elif price > hi:
        verdict = "상대적 고평가 가능성"
    else:
        verdict = "적정 범위"
    band_eval = None
    if band:
        base = scenarios["기준"]["per_share"]
        band_eval = {"low_premium": band["low"] / base - 1, "high_premium": band["high"] / base - 1}

    # 주관사 비교기업 vs 분석용 비교기업
    uw = [p["name"] for p in peers if p.get("origin") == "underwriter"]
    an = [p["name"] for p in peers if p.get("origin") == "analyst"]

    # 회사 제시 미래실적 민감도 (확인 실적 아님)
    proj = _v(facts, "fin.projection")
    sens = None
    if proj and "PER" in used:
        base_per = used["PER"]["scenarios"]["기준"]["multiple"]
        rows = []
        for yr, d in sorted(proj.items()):
            if d.get("net_income") and d["net_income"] > 0:
                for h in config.FORECAST_HAIRCUTS:
                    rows.append({"year": yr, "haircut": h,
                                 "per_share": round(base_per * d["net_income"] * (1 - h) / shares_fd)})
        sens = {"note": "회사 제시 추정 순이익 × 분석용 기준 PER. 할인 0/20/40% 가정. 확인된 실적 아님.", "rows": rows}

    return {
        "verdict": verdict, "price_used": price, "price_label": price_label, "period": period,
        "fin_basis": basis, "shares_fd": shares_fd, "net_debt_post": net_debt_post,
        "proceeds_new": proceeds_new, "methods": methods, "excluded_peers": excluded_peers,
        "scenarios": scenarios, "band_eval": band_eval, "assumptions": assumptions,
        "peer_sets": {"underwriter": uw, "analyst": an, "overlap": sorted(set(uw) & set(an))},
        "projection_sensitivity": sens,
        "rule": "공모가 < 보수 → 저평가 가능성 / 보수~낙관 → 적정 / > 낙관 → 고평가 가능성",
    }


def supply_view(facts: dict) -> dict:
    """상장일 수급 관련 지표(가치평가와 별도). 값이 없으면 '미공개'로 둔다."""
    keys = {
        "float_ratio": "offer.float_ratio", "lockup_commit": "demand.lockup_commit_ratio",
        "demand_ratio": "demand.competition_ratio", "sub_ratio": "subscription.competition_ratio",
    }
    out = {k: _v(facts, v) for k, v in keys.items()}
    total, old = _v(facts, "offer.shares_total"), _v(facts, "offer.shares_old")
    out["old_share_ratio"] = (old / total) if (total and old is not None) else None
    flags = []
    if out["float_ratio"] is not None and out["float_ratio"] >= 0.4:
        flags.append("상장 직후 유통가능물량 40% 이상")
    if out["old_share_ratio"] is not None and out["old_share_ratio"] >= 0.3:
        flags.append("구주매출 비중 30% 이상")
    if out["lockup_commit"] is not None and out["lockup_commit"] < 0.05:
        flags.append("의무보유확약 5% 미만")
    out["flags"] = flags
    out["lockups"] = _v(facts, "offer.lockup") or []
    return out


def _judge(value: float, price: float) -> dict:
    d = 1 - price / value
    if d >= config.FAIR_DISCOUNT_HIGH:
        v = "상대적 저평가 가능성"
    elif d >= config.FAIR_DISCOUNT_LOW:
        v = "적정 범위"
    else:
        v = "상대적 고평가 가능성"
    return {"verdict": v, "eff_discount": d, "premium": price / value - 1}


def underwriter_check(facts: dict) -> dict | None:
    """주관사 공모가 산정 근거를 확인 실적 기준으로 재검토한다.

    - 주관사 주당 평가가액(V_u) = 적용 배수 × 적용 이익 ÷ 공모 후 주식수 (공시·보도값)
    - 확인 실적 기준 가치(V_a): 주관사가 실제 실적을 썼으면 V_u, 조정·추정 이익을 썼으면 확인 실적으로 다시 계산
    - 실효 할인율 = 1 − 공모가 ÷ V_a → 구간 판정(config.FAIR_DISCOUNT_LOW/HIGH)
    - 적정 공모가 범위 = V_a × (1 − 30%) ~ V_a × (1 − 15%)
    """
    u = _v(facts, "valuation.underwriter")
    if not u:
        return None
    src = facts["valuation.underwriter"]
    shares = _v(facts, "offer.shares_post_ipo")
    band = _v(facts, "offer.price_band")
    final = _v(facts, "offer.price_final")
    V_u = u.get("per_share_value")
    if V_u is None and u.get("value_total") and shares:
        V_u = u["value_total"] / shares
    basis = u.get("earnings_basis", "unknown")
    V_a, how = None, None
    if basis == "actual" and V_u:
        V_a, how = V_u, "주관사가 확인된 최근 실적을 적용 → 주관사 평가가액과 동일"
    elif basis == "adjusted" and u.get("actual_value_total") and shares:
        V_a, how = u["actual_value_total"] / shares, "주관사의 조정 이익 대신 조정 전 실제 이익 기준 가치로 재계산"
    elif basis == "forecast" and u.get("actual_earnings") and u["actual_earnings"] > 0 and shares and u.get("method") == "PER":
        V_a, how = u["actual_earnings"] * u["multiple"] / shares, "미래 추정 이익 대신 확인된 최근 순이익 × 같은 PER로 재계산"

    if final:
        prices = [("확정 공모가", final)]
        P = final
    elif band:
        prices = [("희망 하단", band["low"]), ("희망 상단", band["high"])]
        P = (band["low"] + band["high"]) / 2
    else:
        return {"verdict": "판단 유보", "engine": "underwriter_check", "reasons": ["공모가 정보 없음"]}
    price_label = "확정 공모가" if final else "희망밴드 중간값"

    out = {
        "engine": "underwriter_check", "price_used": P, "price_label": price_label, "prices": prices,
        "underwriter": u, "underwriter_value": V_u, "actual_value": V_a, "actual_how": how,
        "source": src.get("source"), "basis_label": src.get("basis"),
        "thresholds": {"low": config.FAIR_DISCOUNT_LOW, "high": config.FAIR_DISCOUNT_HIGH},
        "rule": f"확인 실적 기준 가치 대비 공모가 할인율 ≥{config.FAIR_DISCOUNT_HIGH:.0%} 저평가 가능성 / "
                f"{config.FAIR_DISCOUNT_LOW:.0%}~{config.FAIR_DISCOUNT_HIGH:.0%} 적정 / <{config.FAIR_DISCOUNT_LOW:.0%} 고평가 가능성",
        "reasons": [], "provisional": False,
    }
    if V_u:
        out["reference"] = _judge(V_u, P)  # 회사·주관사 추정을 그대로 믿을 경우
        out["by_price_u"] = [{"label": l, "price": p, **_judge(V_u, p), "required_achievement": p / V_u} for l, p in prices]
    base = V_a
    if base is None and basis == "unknown" and V_u:
        base, out["provisional"] = V_u, True
        out["reasons"].append("주관사 적용 이익이 실제 실적인지 추정치인지 확인되지 않아 주관사 평가가액으로 잠정 판정")
    if base:
        j = _judge(base, P)
        out.update(verdict=j["verdict"], eff_discount=j["eff_discount"], premium=j["premium"],
                   fair_range={"low": base * (1 - config.FAIR_DISCOUNT_HIGH), "high": base * (1 - config.FAIR_DISCOUNT_LOW)},
                   by_price=[{"label": l, "price": p, **_judge(base, p)} for l, p in prices],
                   sensitivity=[{"multiple_factor": m, "value": base * m, **_judge(base * m, P)} for m in (0.8, 1.0, 1.2)])
    else:
        out["verdict"] = "판단 유보"
        if basis == "forecast":
            out["reasons"].append("주관사가 미래 추정 이익을 썼고, 확인된 최근 이익이 적자이거나 미확인이라 실적 기준 가치를 계산할 수 없음")
        if not V_u:
            out["reasons"].append("주관사 주당 평가가액 미확인")
    if V_a and V_u:
        out["forecast_dependency"] = max(0.0, 1 - V_a / V_u)
    elif basis == "forecast":
        out["forecast_dependency"] = 1.0
    return out
