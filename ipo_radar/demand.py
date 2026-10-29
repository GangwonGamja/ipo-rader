"""기관·청약자 수요 지표 — 수요예측과 일반청약 결과로 본 '시장 여론'.

기관 수요 강도(0~100) = Σ(가중치 × 구성요소) ÷ Σ(값이 있는 구성요소 가중치)
  - 경쟁률 40%      : log10(경쟁률) ÷ log10(1500) × 100 (1500:1 이상 100점)
  - 의무보유확약 30%: 확약 비율 ÷ 30% × 100 (30% 이상 100점)
  - 상단 이상 가격 제시 비율 20%: 비율 × 100
  - 확정가 위치 10% : 밴드 초과 100 / 상단 80 / 밴드 내 20~80 / 하단 20 / 하단 미만 0
경쟁률이 없으면(수요예측 결과 발표 전) 점수를 내지 않는다.
등급: 75 이상 강함 · 50~75 보통 · 50 미만 약함. 상장 후 주가를 예측하는 지표가 아니다.
"""
from __future__ import annotations

import math

from . import config


def _v(f, k):
    x = f.get(k)
    return x["value"] if x else None


POSITION_WORD = {"above": 100.0, "top": 80.0, "mid": 50.0, "low": 20.0, "below": 0.0}


def price_position(final, band, word=None):
    if word in POSITION_WORD:
        return POSITION_WORD[word]
    if final is None or not band:
        return None
    lo, hi = band["low"], band["high"]
    if final > hi:
        return 100.0
    if final == hi:
        return 80.0
    if final < lo:
        return 0.0
    if final == lo:
        return 20.0
    return 20 + 60 * (final - lo) / (hi - lo) if hi > lo else 50.0


def demand_view(facts: dict) -> dict:
    W = config.DEMAND_WEIGHTS
    comp = _v(facts, "demand.competition_ratio")
    lock = _v(facts, "demand.lockup_commit_ratio")
    above = _v(facts, "demand.above_top_ratio")
    final, band = _v(facts, "offer.price_final"), _v(facts, "offer.price_band")
    parts = {
        "competition": min(100.0, math.log10(max(comp, 1)) / math.log10(1500) * 100) if comp else None,
        "lockup": min(100.0, lock / 0.30 * 100) if lock is not None else None,
        "above_top": above * 100 if above is not None else None,
        "price_position": price_position(final, band, _v(facts, "offer.price_position")),
    }
    out = {"competition_ratio": comp, "lockup_ratio": lock, "above_top_ratio": above, "final": final, "band": band,
           "sub_ratio": _v(facts, "subscription.competition_ratio"), "sub_deposit": _v(facts, "subscription.deposit"),
           "components": {k: (round(v, 1) if v is not None else None) for k, v in parts.items()}, "weights": W,
           "formula": "경쟁률 40%·의무보유확약 30%·상단 이상 가격 제시 20%·확정가 위치 10%, 값 있는 항목만 재가중"}
    if comp is None:
        out.update(score=None, grade="발표 전", note="수요예측 결과가 아직 없습니다")
        return out
    avail = sum(W[k] for k, v in parts.items() if v is not None)
    score = sum(W[k] * v for k, v in parts.items() if v is not None) / avail
    out.update(score=round(score, 1), coverage=round(avail, 2),
               grade="강함" if score >= 75 else "보통" if score >= 50 else "약함")
    flags = []
    if lock is not None and lock < 0.10:
        flags.append(f"의무보유확약 {lock:.1%}로 낮음 — 상장 초기 기관 매도 가능 물량 많음")
    if (final is not None and band and final <= band["low"]) or _v(facts, "offer.price_position") in ("low", "below"):
        flags.append("확정 공모가가 희망밴드 하단 이하")
    if comp and comp >= 1000:
        flags.append(f"기관 경쟁률 {comp:,.0f} 대 1 — 높은 수요")
    out["flags"] = flags
    return out
