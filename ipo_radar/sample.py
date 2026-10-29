"""화면·산식 시연용 예시 데이터 (전부 가상).

- 기업명 앞에 [예시], is_sample=1, 출처 유형 'sample', URL은 example.invalid.
- 실제 기업 집계·KPI·브리핑과 섞이지 않으며 대시보드에서 '예시 데이터 보기'를 켤 때만 보인다.
- 실데이터 연결 후에는 `--no-sample`로 끈다.
"""
from __future__ import annotations

import random
from datetime import timedelta

from . import interest, registry
from .db import upsert_source

B = 100_000_000  # 1억


def _d(today, n):
    return (today + timedelta(days=n)).isoformat()


def _companies(today):
    mon = today - timedelta(days=today.weekday())
    D = lambda n: _d(mon, n)  # noqa: E731
    peers_tech = [
        {"name": "[예시] 비교A", "origin": "analyst", "reason": "센서 모듈·동일 고객군", "period": "2025", "fin_basis": "연결",
         "mktcap": 3200 * B, "net_debt": -200 * B, "revenue": 1500 * B, "ebitda": 330 * B, "net_income": 210 * B},
        {"name": "[예시] 비교B", "origin": "analyst", "reason": "반도체 검사 부품, 매출 규모 유사", "period": "2025", "fin_basis": "연결",
         "mktcap": 2100 * B, "net_debt": 100 * B, "revenue": 900 * B, "ebitda": 190 * B, "net_income": 120 * B},
        {"name": "[예시] 비교C", "origin": "analyst", "reason": "성장률·이익률 유사", "period": "2025", "fin_basis": "연결",
         "mktcap": 4500 * B, "net_debt": -500 * B, "revenue": 2100 * B, "ebitda": 520 * B, "net_income": 300 * B},
        {"name": "[예시] 비교D", "origin": "analyst", "reason": "적자 → PER 제외 시연", "period": "2025", "fin_basis": "연결",
         "mktcap": 1500 * B, "net_debt": 50 * B, "revenue": 700 * B, "ebitda": 60 * B, "net_income": -30 * B},
        {"name": "[예시] 비교E", "origin": "analyst", "reason": "별도 기준 → 기준 불일치 제외 시연", "period": "2025", "fin_basis": "별도",
         "mktcap": 2600 * B, "net_debt": 0, "revenue": 1000 * B, "ebitda": 210 * B, "net_income": 150 * B},
        {"name": "[예시] 해외대형사", "origin": "underwriter", "reason": "주관사 선정(규모 차이 큼)", "period": "2025", "fin_basis": "연결",
         "mktcap": 90000 * B, "net_debt": 0, "revenue": 30000 * B, "ebitda": 9000 * B, "net_income": 6000 * B},
        {"name": "[예시] 비교B", "origin": "underwriter", "reason": "주관사 선정", "period": "2025", "fin_basis": "연결",
         "mktcap": 2100 * B, "net_debt": 100 * B, "revenue": 900 * B, "ebitda": 190 * B, "net_income": 120 * B},
    ]
    peers_bio = [
        {"name": f"[예시] 바이오{i}", "origin": "analyst", "reason": "플랫폼 바이오·적자·기술이전 매출", "period": "2025",
         "fin_basis": "연결", "mktcap": m * B, "net_debt": nd * B, "revenue": r * B, "ebitda": -50 * B, "net_income": -80 * B}
        for i, (m, nd, r) in enumerate([(5200, -800, 260), (3800, -400, 150), (7000, -1200, 410), (2500, -300, 90)], 1)
    ]
    return [
        {"name": "[예시] 알파센서", "sector": "반도체 센서", "market": "KOSDAQ", "underwriters": "예시증권",
         "buzz": (6, 1.8), "facts": {
             "sched.demand_forecast": {"start": D(-7), "end": D(-3)},
             "sched.subscription": {"start": D(2), "end": D(3)},
             "sched.refund": {"start": D(5), "end": D(5)},
             "sched.listing": {"start": D(11), "end": D(11)},
             "offer.price_band": {"low": 18000, "high": 21000}, "offer.price_final": 21000,
             "offer.shares_total": 2_500_000, "offer.shares_new": 2_000_000, "offer.shares_old": 500_000,
             "offer.shares_post_ipo": 11_000_000, "offer.dilutive_shares": 400_000,
             "offer.float_ratio": 0.33, "offer.fee_rate": 0.03,
             "offer.use_of_proceeds": [{"purpose": "시설자금", "amount": 250 * B}, {"purpose": "연구개발", "amount": 150 * B}],
             "offer.lockup": [{"holder": "최대주주 등", "shares": 5_200_000, "release": D(11 + 365)},
                              {"holder": "벤처금융", "shares": 1_100_000, "release": D(11 + 30)}],
             "demand.competition_ratio": 912.4, "demand.lockup_commit_ratio": 0.21, "demand.above_top_ratio": 0.93,
             "subscription.competition_ratio": 845.2, "subscription.deposit": 4.1e12,
             "fin.basis": "연결",
             "fin.annual": {"2023": {"revenue": 520 * B, "op_income": 61 * B, "net_income": 48 * B, "op_cf": 55 * B, "ebitda": 85 * B, "equity": 400 * B, "liabilities": 300 * B, "cash": 80 * B, "debt": 150 * B},
                            "2024": {"revenue": 690 * B, "op_income": 95 * B, "net_income": 72 * B, "op_cf": 80 * B, "ebitda": 120 * B, "equity": 470 * B, "liabilities": 320 * B, "cash": 110 * B, "debt": 160 * B},
                            "2025": {"revenue": 910 * B, "op_income": 150 * B, "net_income": 118 * B, "op_cf": 122 * B, "ebitda": 182 * B, "equity": 590 * B, "liabilities": 340 * B, "cash": 140 * B, "debt": 170 * B}},
             "fin.one_off": {"2025": {"net_income_adj": -8 * B, "note": "유형자산 처분이익 제외"}},
             "fin.quarter": {"period": "2026Q2 누적", "revenue": 520 * B, "op_income": 88 * B, "net_income": 70 * B, "op_cf": 60 * B},
             "fin.top_customer_share": 0.42,
             "fin.projection": {"2027": {"net_income": 210 * B}, "2028": {"net_income": 260 * B}},
             "profile.business": "반도체 공정 모니터링용 센서 모듈 설계·제조", "profile.products": "플라즈마 센서, 웨이퍼 온도 센서",
             "profile.customers": "국내 메모리 제조사 2곳", "profile.competitors": "[예시] 비교A, [예시] 비교C",
             "profile.industry": "공정 미세화로 인라인 계측 수요 증가(회사 전망)",
             "valuation.peers": peers_tech}},
        {"name": "[예시] 베타바이오", "sector": "바이오 플랫폼", "market": "KOSDAQ", "underwriters": "예시증권",
         "buzz": (4, 1.1), "facts": {
             "sched.demand_forecast": {"start": D(1), "end": D(5)},
             "sched.subscription": {"start": D(9), "end": D(10)},
             "sched.listing": {"start": D(18), "end": D(18)},
             "offer.price_band": {"low": 24000, "high": 28000},
             "offer.shares_total": 3_000_000, "offer.shares_new": 3_000_000, "offer.shares_old": 0,
             "offer.shares_post_ipo": 16_000_000, "offer.float_ratio": 0.45,
             "fin.basis": "연결",
             "fin.annual": {"2023": {"revenue": 40 * B, "op_income": -120 * B, "net_income": -125 * B, "op_cf": -100 * B, "ebitda": -100 * B, "equity": 600 * B, "liabilities": 150 * B, "cash": 400 * B, "debt": 50 * B},
                            "2024": {"revenue": 95 * B, "op_income": -140 * B, "net_income": -150 * B, "op_cf": -120 * B, "ebitda": -115 * B, "equity": 520 * B, "liabilities": 160 * B, "cash": 330 * B, "debt": 60 * B},
                            "2025": {"revenue": 180 * B, "op_income": -90 * B, "net_income": -95 * B, "op_cf": -70 * B, "ebitda": -65 * B, "equity": 450 * B, "liabilities": 170 * B, "cash": 260 * B, "debt": 60 * B}},
             "fin.projection": {"2028": {"net_income": 400 * B}},
             "profile.business": "항체 플랫폼 기술이전", "valuation.peers": peers_bio}},
        {"name": "[예시] 감마로보틱스", "sector": "협동로봇", "market": "KOSDAQ", "underwriters": "예시증권",
         "buzz": (9, 2.6), "facts": {
             "sched.demand_forecast": {"start": D(14), "end": D(18)},
             "sched.subscription": {"start": D(21), "end": D(22)},
             "offer.price_band": {"low": 7500, "high": 9000},
             "offer.shares_total": 2_000_000, "offer.shares_new": 1_200_000, "offer.shares_old": 800_000,
             "offer.shares_post_ipo": 9_000_000, "offer.float_ratio": 0.41,
             "fin.basis": "연결",
             "fin.annual": {"2023": {"revenue": 300 * B, "op_income": 20 * B, "net_income": 15 * B, "ebitda": 35 * B, "equity": 250 * B, "liabilities": 600 * B, "cash": 50 * B, "debt": 400 * B},
                            "2024": {"revenue": 360 * B, "op_income": 28 * B, "net_income": 20 * B, "ebitda": 45 * B, "equity": 270 * B, "liabilities": 620 * B, "cash": 60 * B, "debt": 410 * B},
                            "2025": {"revenue": 420 * B, "op_income": 35 * B, "net_income": 25 * B, "ebitda": 55 * B, "equity": 290 * B, "liabilities": 640 * B, "cash": 70 * B, "debt": 420 * B}},
             "profile.business": "제조용 협동로봇", "valuation.peers": peers_tech}},
        {"name": "[예시] 델타플랫폼", "sector": "B2B SaaS", "underwriters": "예시증권",
         "buzz": (1, 3.0), "facts": {
             "offer.price_band": {"low": 9000, "high": 11000},
             "profile.business": "중소기업 회계 SaaS",
             "valuation.peers": peers_tech[:2]}},
        {"name": "[예시] 엡실론스팩1호", "sector": "스팩", "underwriters": "예시증권", "buzz": (0, 1.0), "facts": {
            "sched.subscription": {"start": D(3), "end": D(4)}, "offer.price_band": {"low": 2000, "high": 2000}}},
    ]


TITLES = ["{n} 수요예측 앞두고 기관 관심 확대", "{n} 공모 흥행 기대감… 업계 주목", "{n} 상장 앞두고 고평가 논란",
          "{n} 청약 일정·공모가 정리", "{n} IPO 기업설명회 개최", "{n} 공모가 상단 확정 전망", "{n} 구주매출 부담 우려",
          "{n} 성장성 vs 밸류 부담", "공모주 {n} 청약 후기", "{n} 의무보유확약 비율은?"]
WORDS = ["실적", "밸류에이션", "기관", "확약", "경쟁률", "증설", "고객사", "수주", "마진", "부채", "현금흐름", "보호예수",
         "유통물량", "구주", "신주", "업황", "반도체", "수출", "점유율", "R&D", "특허", "임상", "기술이전", "로봇", "AI",
         "환율", "금리", "따상", "적정가", "비교기업", "할인율", "주관사", "청약증거금", "배정", "균등", "비례"]
PUBS = {"video": ["예시공모주TV", "예시주식채널", "예시재테크"], "news": ["예시경제", "예시일보", "예시투데이", "예시비즈", "예시데일리", "예시IB"],
        "blog": ["blog.example/a", "blog.example/b", "blog.example/c"], "cafe": ["예시주식카페", "예시공모주카페"]}


def _mentions(name, today, base, growth, rnd):
    items = []
    for age in range(0, 45):
        day = today - timedelta(days=age)
        mult = growth if age < 7 else 1.0
        for ch, w in (("news", 1.0), ("blog", 0.7), ("cafe", 0.5), ("video", 0.25)):
            lam = base * w * mult / 3
            k = sum(1 for _ in range(12) if rnd.random() < lam / 12)
            for j in range(k):
                pub = rnd.choice(PUBS[ch])
                title = rnd.choice(TITLES).format(n=name)
                body = " ".join(rnd.sample(WORDS, 6))
                items.append({"channel": ch, "url": f"https://example.invalid/{ch}/{name}/{day.isoformat()}/{j}",
                              "title": title, "snippet": f"{body} — 예시 데이터(가상) {rnd.randint(1, 99999)}",
                              "published_at": day.isoformat() + "T09:00:00+09:00", "publisher": pub,
                              "views": (rnd.randint(2000, 60000) if ch == "video" else rnd.randint(50, 3000)) if ch != "news" else None,
                              "comments": rnd.randint(0, 40) if ch != "news" else None,
                              "likes": rnd.randint(0, 60) if ch != "news" else None})
        if age < 7 and base > 3 and rnd.random() < 0.3:  # 전재 기사·홍보 반복글 시연
            items.append({"channel": "news", "url": f"https://example.invalid/copy/{name}/{day}",
                          "title": f"{name} 수요예측 앞두고 기관 관심 확대", "snippet": f"{name} 수요예측 앞두고 기관 관심 확대 — 전재",
                          "published_at": day.isoformat() + "T10:00:00+09:00", "publisher": "예시전재", "views": None,
                          "comments": None, "likes": None})
    if base > 5:
        for j in range(3):
            items.append({"channel": "cafe", "url": f"https://example.invalid/promo/{name}/{today}/{j}",
                          "title": f"{name} 공모주 무료 리딩방 안내", "snippet": f"{name} 공모 무료 리딩방 오픈채팅 참여",
                          "published_at": today.isoformat() + "T08:00:00+09:00", "publisher": "홍보계정",
                          "views": 10, "comments": 0, "likes": 0})
    return items


def load(r):
    n = 0
    for c in _companies(r.today):
        sid = upsert_source(r.conn, f"https://example.invalid/sample/{c['name']}", "sample", "예시 데이터(가상)",
                            "IPO Radar 예시", r.today.isoformat(), r.at)
        uid, _ = registry.resolve_company(r.conn, c["name"], run_id=r.run_id, source_id=sid, source_type="sample",
                                          is_sample=True, at=r.at, sector=c.get("sector"), market=c.get("market"),
                                          underwriters=c.get("underwriters"))
        for k, v in c["facts"].items():
            basis = "company_projection" if k in ("fin.projection", "profile.industry") else (
                "analyst_estimate" if k == "valuation.peers" else "confirmed")
            registry.set_fact(r.conn, uid, k, v, source_id=sid, run_id=r.run_id, basis=basis,
                              as_of=r.today.isoformat(), at=r.at)
        rnd = random.Random(f"{c['name']}|{r.today.isoformat()}")
        base, g = c["buzz"]
        items = _mentions(c["name"], r.today, base, g, rnd) if base else []
        interest.process_mentions(r.conn, uid, c["name"], items, None, at=r.at)
        r.channels_ok[uid] = {"news", "blog", "cafe", "video"}
        for i in range(60):  # 예시 검색 지수
            day = (r.today - timedelta(days=59 - i)).isoformat()
            val = max(0.0, base * 4 * (1 + (g - 1) * max(0, i - 52) / 7) * (0.7 + 0.6 * rnd.random()))
            r.conn.execute("""INSERT INTO search_trends(uid,day,value,collected_at) VALUES(?,?,?,?)
                              ON CONFLICT(uid,day) DO UPDATE SET value=excluded.value""", (uid, day, round(val, 1), r.at))
        n += 1
    return {"sample_companies": n}
