"""시장 관심도: 언급 정제 + 0~100 관심도 점수 + 별도 감성 요약.

점수 = Σ(가중치 × 구성요소 점수) / Σ(산출 가능한 구성요소 가중치)
  - 언급량(35%)   : 채널별 최근 7일 고유 문서 수를 log 스케일로, 같은 단계 비교집단의 최댓값(또는 하한) 대비 백분율.
                    뉴스·블로그·카페를 각각 정규화한 뒤 평균 → 채널 규모 차이 제거.
  - 증가세(30%)   : ln((최근7일+k)/(직전7일+k)), k=5 (소표본 급증 보정) → 50+50·tanh(x/ln3).
                    3배 증가 ≈ 88점, 변화 없음 = 50점, 1/3 감소 ≈ 12점.
  - 출처 다양성(20%): 30일 고유 매체·커뮤니티의 유효 개수 exp(엔트로피) → log 스케일 백분율.
  - 참여도(15%)   : 조회·댓글·추천이 공개된 문서만 사용. 제공되지 않으면 '산출 불가'로 두고 가중치 재배분.
수집하지 못한 채널·구성요소는 0점이 아니라 '산출 불가'로 처리한다.
산출 가능한 가중치 합이 50% 미만이면 점수를 내지 않는다(산출 보류).
이 점수는 화제성 지표이며 주가 상승 확률이 아니다.
"""
from __future__ import annotations

import math
import re
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta

from . import config
from .db import dumps, iso, sha
from .registry import normalize_name

IPO_CONTEXT = re.compile(r"공모|상장|청약|수요예측|IPO|주관|코스닥|코스피|확약|경쟁률", re.I)
PROMO = re.compile(r"무료\s*(리딩|추천)|리딩방|카톡방|오픈채팅|단톡|텔레그램\s*방|수익\s*보장|급등\s*종목\s*추천|광고|협찬|원고료")
POS = ["흥행", "호조", "성장", "기대", "최대", "상단", "초과", "돌파", "강세", "수혜", "흑자", "인기", "몰려", "대박", "확대"]
NEG = ["부진", "미달", "하단", "철회", "연기", "적자", "우려", "논란", "고평가", "약세", "급락", "손실", "리스크", "정정", "부담"]
STOP = {"공모", "상장", "청약", "수요예측", "코스닥", "코스피", "기업", "증권", "주관", "공모가", "IPO", "희망", "일반",
        "투자자", "진행", "예정", "확정", "오늘", "이번", "주", "등", "및", "위해", "통해", "대한", "앞두고", "관련", "정리",
        "후기", "개최", "전망", "예시"}
CHANNEL_FLOOR = {"news": 20, "blog": 20, "cafe": 10, "video": 10, "community": 10}
CHANNEL_W = {"news": 0.40, "blog": 0.20, "cafe": 0.15, "video": 0.20, "community": 0.05}
CHANNEL_LABEL = {"news": "뉴스", "blog": "블로그", "cafe": "카페", "video": "유튜브", "community": "커뮤니티"}

# 논점별 여론 분류: 글에 포함된 키워드로 논점을 붙이고, 같은 글의 긍·부정을 그 논점에 반영
ASPECTS = {
    "공모가·밸류": ["공모가", "밸류", "고평가", "저평가", "PER", "몸값", "시가총액", "할인율", "비싸", "적정가", "멀티플"],
    "수요예측·청약": ["수요예측", "경쟁률", "증거금", "청약", "확약", "배정", "균등", "비례", "따상"],
    "수급·물량": ["유통", "보호예수", "오버행", "구주", "FI", "벤처캐피탈", "물량", "락업", "매도"],
    "실적·성장": ["매출", "영업이익", "흑자", "적자", "성장", "실적", "수주", "순이익", "마진"],
    "사업·기술": ["기술", "특허", "제품", "고객", "점유율", "플랫폼", "수출", "공급", "기술이전", "임상"],
    "업황·테마": ["반도체", "AI", "방산", "바이오", "조선", "데이터센터", "업황", "테마", "로봇"],
}


def aspects_of(text: str) -> list[str]:
    return [a for a, kws in ASPECTS.items() if any(k.lower() in text.lower() for k in kws)]


def norm_url(u: str) -> str:
    p = urllib.parse.urlparse(u or "")
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "from"))]
    return urllib.parse.urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", urllib.parse.urlencode(q), ""))


def _shingles(t: str, n: int = 3) -> set:
    t = re.sub(r"\W+", "", t or "")
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def sentiment(text: str) -> str:
    p = sum(text.count(w) for w in POS)
    n = sum(text.count(w) for w in NEG)
    return "pos" if p > n else "neg" if n > p else "neu"


def process_mentions(conn, uid: str, name: str, items: list[dict], exclude_terms: list[str] | None,
                     at: str | None = None) -> int:
    """관련성·중복·홍보성·감성 판정 후 저장(upsert). 반환: 저장 건수."""
    at = at or iso()
    norm = normalize_name(name)
    ambiguous = len(norm) <= 2 or bool(exclude_terms)
    existing = [dict(r) for r in conn.execute(
        "SELECT mention_id, title, snippet, publisher, dup_group FROM mentions WHERE uid=? AND relevant=1", (uid,))]
    sh_cache = {e["mention_id"]: _shingles((e["title"] or "") + (e["snippet"] or "")[:80]) for e in existing}
    n = 0
    for it in items:
        if not it.get("url"):
            continue
        mid = sha(it["channel"] + "|" + norm_url(it["url"]))
        text = f"{it.get('title') or ''} {it.get('snippet') or ''}"
        flat = normalize_name(text)
        relevant, reason = 1, None
        if norm not in flat:
            relevant, reason = 0, "기업명 미포함"
        elif any(t.lower() in text.lower() for t in (exclude_terms or [])):
            relevant, reason = 0, "제외어 포함(동명이인·관계사·다른 의미)"
        elif ambiguous and not IPO_CONTEXT.search(text):
            relevant, reason = 0, "IPO 맥락 없음(동음이의 가능)"
        promo = 1 if PROMO.search(text) else 0
        sh = _shingles((it.get("title") or "") + (it.get("snippet") or "")[:80])
        group, dup = mid, 0
        if relevant:
            for eid, esh in sh_cache.items():
                if eid != mid and _jaccard(sh, esh) >= 0.8:
                    row = conn.execute("SELECT dup_group FROM mentions WHERE mention_id=?", (eid,)).fetchone()
                    group, dup = (row["dup_group"] if row and row["dup_group"] else eid), 1
                    break
            sh_cache[mid] = sh
        conn.execute(
            """INSERT INTO mentions(mention_id,uid,channel,publisher,url,title,snippet,published_at,views,comments,likes,
                 dup_group,is_duplicate,is_promo,relevant,exclusion_reason,sentiment,collected_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(mention_id) DO UPDATE SET views=COALESCE(excluded.views,views),
                 comments=COALESCE(excluded.comments,comments), likes=COALESCE(excluded.likes,likes),
                 collected_at=excluded.collected_at""",
            (mid, uid, it["channel"], it.get("publisher"), it["url"], it.get("title"), it.get("snippet"),
             it.get("published_at"), it.get("views"), it.get("comments"), it.get("likes"),
             group, dup, promo, relevant, reason, sentiment(text), at))
        n += 1
    # 같은 출처가 거의 같은 글을 3건 이상 반복하면 홍보성 반복으로 표시
    conn.execute("""UPDATE mentions SET is_promo=1 WHERE uid=? AND dup_group IN (
                      SELECT dup_group FROM mentions WHERE uid=? AND relevant=1
                      GROUP BY dup_group, publisher HAVING COUNT(*)>=3)""", (uid, uid))
    return n


def _parse_dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=config.KST)


def window_stats(conn, uid: str, now: datetime, channels_ok: set[str], name: str = "") -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM mentions WHERE uid=? AND relevant=1", (uid,))]
    total_raw = len(rows)
    uniq = [r for r in rows if not r["is_duplicate"]]
    st = {"channels": {}, "dup_count": total_raw - len(uniq),
          "promo_count": sum(r["is_promo"] for r in rows), "channels_ok": sorted(channels_ok)}
    pubs30 = Counter()
    eng_docs, eng_sum = 0, 0.0
    sent = Counter()
    topics = Counter()
    reps = []
    asp: dict[str, dict] = {}
    ch_eng: dict[str, dict] = {}
    for ch in sorted(channels_ok):
        st["channels"][ch] = {"c7": 0, "p7": 0, "c30": 0, "p30": 0}
    for r in uniq:
        d = _parse_dt(r["published_at"])
        if d is None or r["channel"] not in st["channels"]:
            continue
        age = (now - d).days
        c = st["channels"][r["channel"]]
        if age < 7:
            c["c7"] += 1
        elif age < 14:
            c["p7"] += 1
        if age < 30:
            c["c30"] += 1
            pubs30[(r["channel"], r["publisher"] or "?")] += 1
            if r["views"] is not None or r["comments"] is not None or r["likes"] is not None:
                e = ch_eng.setdefault(r["channel"], {"docs": 0, "views": 0, "comments": 0, "likes": 0})
                e["docs"] += 1
                for k in ("views", "comments", "likes"):
                    e[k] += r[k] or 0
            if not r["is_promo"]:
                for a in aspects_of(f"{r['title'] or ''} {r['snippet'] or ''}"):
                    x = asp.setdefault(a, {"aspect": a, "n": 0, "pos": 0, "neu": 0, "neg": 0, "examples": []})
                    x["n"] += 1
                    x[r["sentiment"]] += 1
                    if len(x["examples"]) < 2:
                        x["examples"].append({"title": r["title"], "url": r["url"], "publisher": r["publisher"],
                                              "channel": r["channel"], "sentiment": r["sentiment"]})
                sent[r["sentiment"]] += 1
                for w in re.findall(r"[가-힣A-Za-z]{2,}", r["title"] or ""):
                    if w not in STOP and normalize_name(w) not in normalize_name(name) and w not in (r["publisher"] or ""):
                        topics[w] += 1
                reps.append(r)
            if any(r[k] is not None for k in ("views", "comments", "likes")):
                eng_docs += 1
                eng_sum += (r["comments"] or 0) + (r["likes"] or 0) + (r["views"] or 0) / 100
        elif age < 60:
            c["p30"] += 1
    probs = [v / sum(pubs30.values()) for v in pubs30.values()] if pubs30 else []
    st["publishers_30d"] = len(pubs30)
    st["effective_sources"] = math.exp(-sum(p * math.log(p) for p in probs)) if probs else 0.0
    st["engagement"] = {"docs": eng_docs, "per_doc": (eng_sum / eng_docs) if eng_docs else None}
    tot = sum(sent.values())
    st["sentiment"] = {k: round(sent[k] / tot, 3) for k in ("pos", "neu", "neg")} if tot else None
    st["sentiment_n"] = tot
    st["topics"] = [w for w, _ in topics.most_common(6)]
    st["aspects"] = sorted(asp.values(), key=lambda x: -x["n"])
    st["channel_engagement"] = ch_eng
    reps.sort(key=lambda r: r["published_at"] or "", reverse=True)
    seen_pub, rep_out = set(), []
    for r in reps:
        if r["publisher"] in seen_pub:
            continue
        seen_pub.add(r["publisher"])
        rep_out.append({"title": r["title"], "url": r["url"], "publisher": r["publisher"],
                        "channel": r["channel"], "published_at": r["published_at"],
                        "summary": (r["snippet"] or "")[:120], "sentiment": r["sentiment"]})
        if len(rep_out) >= 6:
            break
    st["representative"] = rep_out
    c7 = sum(c["c7"] for c in st["channels"].values())
    p7 = sum(c["p7"] for c in st["channels"].values())
    c30 = sum(c["c30"] for c in st["channels"].values())
    p30 = sum(c["p30"] for c in st["channels"].values())
    st.update({"c7": c7, "p7": p7, "c30": c30, "p30": p30,
               "chg7": ((c7 - p7) / p7) if p7 else None, "chg30": ((c30 - p30) / p30) if p30 else None})
    return st


def _log_pct(v: float, ref: float) -> float:
    return 100.0 * min(1.0, math.log1p(max(v, 0)) / math.log1p(ref)) if ref > 0 else 0.0


def score_group(stats: dict[str, dict]) -> dict[str, dict]:
    """같은 기간·비슷한 IPO 단계 기업들의 통계를 받아 점수를 계산."""
    W = config.INTEREST_WEIGHTS
    peers_n = len(stats)
    ref_ch: dict[str, float] = {}
    for s in stats.values():
        for ch, c in s["channels"].items():
            ref_ch[ch] = max(ref_ch.get(ch, CHANNEL_FLOOR.get(ch, 10)), c["c7"])
    ref_div = max([10.0] + [s["effective_sources"] for s in stats.values()])
    eng_vals = [s["engagement"]["per_doc"] for s in stats.values() if s["engagement"]["per_doc"] is not None]
    ref_eng = max(eng_vals) if eng_vals else None
    out = {}
    for uid, s in stats.items():
        comp: dict[str, float | None] = {}
        chs = s["channels"]
        if chs:
            wsum = sum(CHANNEL_W.get(ch, 0.1) for ch in chs)
            comp["volume"] = sum(CHANNEL_W.get(ch, 0.1) * _log_pct(c["c7"], ref_ch[ch]) for ch, c in chs.items()) / wsum
            k = config.GROWTH_PRIOR
            x = math.log((s["c7"] + k) / (s["p7"] + k))
            comp["growth"] = 50 + 50 * math.tanh(x / math.log(3))
            comp["diversity"] = _log_pct(s["effective_sources"], ref_div)
        else:
            comp["volume"] = comp["growth"] = comp["diversity"] = None
        per = s["engagement"]["per_doc"]
        comp["engagement"] = _log_pct(per, ref_eng) if (per is not None and ref_eng) else None
        avail = sum(W[k] for k, v in comp.items() if v is not None)
        score = (sum(W[k] * v for k, v in comp.items() if v is not None) / avail) if avail >= config.MIN_COMPONENT_COVERAGE else None
        n30 = s["c30"]
        if score is None:
            rel = "산출 불가"
        elif n30 >= 30 and len(chs) >= 2 and avail >= 0.85:
            rel = "높음"
        elif n30 >= 10 and len(chs) >= 1:
            rel = "보통"
        else:
            rel = "낮음"
        small = (s["c7"] + s["p7"]) < 10
        out[uid] = {
            "score": round(score, 1) if score is not None else None,
            "components": {k: (round(v, 1) if v is not None else None) for k, v in comp.items()},
            "weights": W, "weight_coverage": round(avail, 2),
            "reliability": rel, "small_sample": small, "peer_group_size": peers_n,
            "spike": bool(score is not None and comp["growth"] is not None and comp["growth"] >= 75 and s["c7"] >= 5),
            "stats": s,
            "formula": "Σ(w·s)/Σw(산출 가능), 언급량35·증가세30·다양성20·참여도15, 증가세 k=5 보정",
            "disclaimer": "관심도는 언급·확산 정도이며 주가 상승 확률이 아님",
        }
    return out


def save_scores(conn, run_id: str, scored: dict, at: str | None = None):
    for uid, d in scored.items():
        conn.execute(
            """INSERT INTO interest_scores(uid,run_id,score,detail,computed_at) VALUES(?,?,?,?,?)
               ON CONFLICT(uid,run_id) DO UPDATE SET score=excluded.score, detail=excluded.detail,
                 computed_at=excluded.computed_at""",
            (uid, run_id, d["score"], dumps(d), at or iso()))


def stage_of(facts: dict, today) -> str:
    """IPO 단계: watch / filed / demand_forecast / subscription / pre_listing / listed / withdrawn."""
    t = today.isoformat()
    def rng(k):
        v = (facts.get(k) or {}).get("value") or {}
        return v.get("start"), v.get("end")
    ls, _ = rng("sched.listing")
    ss, se = rng("sched.subscription")
    ds, de = rng("sched.demand_forecast")
    if ls and ls <= t:
        return "listed"
    if se and se < t:
        return "pre_listing"
    if ss and (ss <= t <= (se or ss) or ss <= (today + timedelta(days=14)).isoformat()):
        return "subscription"
    if de and (de >= t) or (ds and ds >= t):
        return "demand_forecast"
    if ss or ds or ls:
        return "filed"
    return "watch"


STAGE_LABEL = {"watch": "관찰(일정 미정)", "filed": "신고서 제출", "demand_forecast": "수요예측",
               "subscription": "청약 임박·진행", "pre_listing": "상장 대기", "listed": "상장 완료",
               "withdrawn": "철회", "postponed": "연기"}
STAGE_BUCKET = {"watch": "far", "filed": "far", "demand_forecast": "near", "subscription": "near",
                "pre_listing": "near", "listed": "done", "withdrawn": "done", "postponed": "far"}
