"""IPO Radar 검증 테스트 (python -m unittest -v).

네트워크 없이 교체 가능한 전송 계층(fake transport)으로 주간 실행을 모의한다.
IPOSTOCK HTML 픽스처는 '달력 칸 + [구분]기업명' 형태의 합성 예시이며, 실제 페이지 구조는
접속이 허용된 환경에서 `python -m ipo_radar probe ipostock`으로 확인해야 한다.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from ipo_radar import config, http, interest, registry, valuation
from ipo_radar.__main__ import next_run, run_with_recovery
from ipo_radar.collectors import ipostock
from ipo_radar.db import connect

KST = config.KST


def cal_html(entries: dict[int, list[str]]) -> str:
    """합성 달력: {일: ['[청약]바로팜', ...]}"""
    tds = []
    for d in range(1, 31):
        items = "".join(f'<br><a href="/view.asp?code={abs(hash(e)) % 9999}">{e}</a>' for e in entries.get(d, []))
        tds.append(f"<td><span>{d}</span>{items}</td>")
    rows = "".join("<tr>" + "".join(tds[i:i + 7]) + "</tr>" for i in range(0, 30, 7))
    return f"<html><body><table>{rows}</table></body></html>"


class FakeNet:
    """URL별 응답 지정. 함수면 호출 결과(status, body)를 사용."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.calls: list[str] = []
        self.down: set[str] = set()

    def __call__(self, url, headers):
        self.calls.append(url)
        for host in self.down:
            if host in url:
                raise OSError("network unreachable")
        if url.endswith("/robots.txt"):
            return 404, b""
        for k, v in self.routes.items():
            if k in url:
                return (200, v.encode("utf-8")) if isinstance(v, str) else v
        return 404, b""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (config.SEED_DIR, config.MANUAL_DIR)
        config.SEED_DIR = self.tmp / "seed"
        config.MANUAL_DIR = self.tmp / "manual"
        config.SEED_DIR.mkdir()
        config.MANUAL_DIR.mkdir()
        self.db = self.tmp / "t.sqlite3"
        self.net = FakeNet()
        http.set_transport(self.net, sleep=lambda s: None)

    def tearDown(self):
        http.set_transport(None)
        config.SEED_DIR, config.MANUAL_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_at(self, iso, **kw):
        from ipo_radar import pipeline
        return pipeline.run(now=datetime.fromisoformat(iso), trigger="test", db_path=self.db, with_sample=False,
                            export_dir=self.tmp / "out", **kw)


class TestMonths(unittest.TestCase):
    def test_current_month_plus_90_days(self):
        self.assertEqual(ipostock.months_to_scan(date(2026, 10, 29)), [(2026, 10), (2026, 11), (2026, 12), (2027, 1)])
        self.assertEqual(ipostock.months_to_scan(date(2026, 11, 2)), [(2026, 11), (2026, 12), (2027, 1)])
        self.assertEqual(ipostock.months_to_scan(date(2026, 12, 31)), [(2026, 12), (2027, 1), (2027, 2), (2027, 3)])

    def test_url_is_dynamic(self):
        self.assertEqual(ipostock.month_url(2027, 1), "http://www.ipostock.co.kr/sub03/ipo04.asp?str1=2027&str2=1")


class TestParser(unittest.TestCase):
    def test_parse_and_merge(self):
        html = cal_html({2: ["[청약]바로팜", "[수요]케이티이"], 3: ["[청약]바로팜", "[환불]넥스아이"],
                         10: ["다비오(상장)"], 11: ["관련없는 공지"]})
        rows = {(r["name"], r["event"]): r for r in ipostock.parse_calendar(html, 2026, 11)}
        self.assertEqual(rows[("바로팜", "sched.subscription")]["start"], "2026-11-02")
        self.assertEqual(rows[("바로팜", "sched.subscription")]["end"], "2026-11-03")
        self.assertIn(("케이티이", "sched.demand_forecast"), rows)
        self.assertIn(("넥스아이", "sched.refund"), rows)
        self.assertIn(("다비오", "sched.listing"), rows)
        self.assertEqual(len(rows), 4)

    def test_cross_month_merge(self):
        a = ipostock.parse_calendar(cal_html({30: ["[수요]A사"]}), 2026, 10)
        b = ipostock.parse_calendar(cal_html({2: ["[수요]A사"]}), 2026, 11)
        m = ipostock.merge_months(a + b)
        self.assertEqual((m[0]["start"], m[0]["end"]), ("2026-10-30", "2026-11-02"))


class TestIdentity(Base):
    def test_alias_dedupe_and_kind(self):
        conn = connect(self.db)
        u1, c1 = registry.resolve_company(conn, "(주)바로팜", source_type="aggregator")
        u2, c2 = registry.resolve_company(conn, "바로팜 주식회사", corp_code="01234567", source_type="official_disclosure")
        u3, c3 = registry.resolve_company(conn, "Baropharm-unrelated", corp_code="01234567")
        self.assertTrue(c1 and not c2 and not c3)
        self.assertEqual(u1, u2)
        self.assertEqual(u1, u3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 1)
        self.assertEqual(registry.classify_kind("미래에셋비전스팩7호"), "spac")
        self.assertEqual(registry.classify_kind("한국리츠"), "reits")
        self.assertEqual(registry.classify_kind("바로팜"), "general")

    def test_lower_source_cannot_overwrite_official(self):
        conn = connect(self.db)
        from ipo_radar.db import upsert_source
        off = upsert_source(conn, "https://dart/x", "official_disclosure")
        web = upsert_source(conn, "https://news/x", "websearch_summary")
        uid, _ = registry.resolve_company(conn, "A사")
        self.assertEqual(registry.set_fact(conn, uid, "sched.subscription", {"start": "2026-11-02", "end": "2026-11-03"}, source_id=off), "new")
        self.assertEqual(registry.set_fact(conn, uid, "sched.subscription", {"start": "2026-11-09", "end": "2026-11-10"}, source_id=web), "conflict")
        self.assertEqual(registry.get_facts(conn, uid)["sched.subscription"]["value"]["start"], "2026-11-02")
        registry.set_fact(conn, uid, "sched.subscription", {"start": "2026-11-09", "end": "2026-11-10"}, source_id=web)
        n = conn.execute("SELECT COUNT(*) FROM changes WHERE change_type='source_conflict'").fetchone()[0]
        self.assertEqual(n, 1, "같은 불일치는 한 번만 기록")


class TestWeekly(Base):
    """4주 모의 운영: 신규 → 재실행(중복 없음) → 일정 변경·이탈 → 수집 실패 → 복구."""

    def set_ipostock(self, by_month: dict[tuple[int, int], dict]):
        self.net.routes = {f"str1={y}&str2={m}": cal_html(e) for (y, m), e in by_month.items()}
        self.net.routes.setdefault("ipo04.asp", cal_html({}))

    def count(self, sql):
        conn = connect(self.db)
        try:
            return conn.execute(sql).fetchone()[0]
        finally:
            conn.close()

    def test_four_weeks(self):
        # 1주차 (11/2 월 08:00)
        self.set_ipostock({(2026, 11): {9: ["[청약]알파", "[수요]베타"], 10: ["[청약]알파"], 11: ["[수요]베타"], 18: ["[상장]알파"]}})
        r1 = self.run_at("2026-11-02T08:00:00+09:00")
        self.assertIn(r1["status"], ("success", "partial"))
        self.assertEqual(r1["stages"]["ipostock"]["rows"], 3)
        self.assertEqual(self.count("SELECT COUNT(*) FROM companies"), 2)
        self.assertEqual(self.count("SELECT COUNT(*) FROM changes WHERE change_type='new'"), 2)
        self.assertEqual(self.count("SELECT COUNT(*) FROM changes WHERE change_type='schedule_change'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM reports"), 2)
        months_called = [u for u in self.net.calls if "ipo04.asp" in u]
        self.assertEqual(len(months_called), 3, "11월~1월 3개 월 조회")

        # 같은 주 재실행 → 아무것도 늘지 않음
        before = {t: self.count(f"SELECT COUNT(*) FROM {t}") for t in ("companies", "changes", "reports", "briefings")}
        self.run_at("2026-11-02T09:00:00+09:00")
        after = {t: self.count(f"SELECT COUNT(*) FROM {t}") for t in before}
        self.assertEqual(before, after)

        # 2주차: 알파 청약 연기(9~10 → 16~17), 베타 사라짐, 감마 신규
        self.set_ipostock({(2026, 11): {16: ["[청약]알파"], 17: ["[청약]알파"], 25: ["[상장]알파"], 20: ["[수요]감마"]}})
        self.run_at("2026-11-09T08:00:00+09:00")
        conn = connect(self.db)
        sc = conn.execute("""SELECT c.field, c.before, c.after FROM changes c JOIN companies co USING(uid)
                             WHERE co.name='알파' AND change_type='schedule_change' ORDER BY id""").fetchall()
        self.assertEqual([x["field"] for x in sc], ["sched.subscription", "sched.listing"])
        self.assertEqual(json.loads(sc[0]["before"])["start"], "2026-11-09")
        self.assertEqual(json.loads(sc[0]["after"])["start"], "2026-11-16")
        self.assertEqual(self.count("SELECT COUNT(*) FROM changes c JOIN companies co USING(uid) WHERE co.name='베타' AND change_type='postponed'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM changes c JOIN companies co USING(uid) WHERE co.name='감마' AND change_type='new'"), 1)
        self.assertEqual(self.count("SELECT MAX(version) FROM reports r JOIN companies co USING(uid) WHERE co.name='알파'"), 2)
        rep = json.loads(conn.execute("SELECT diff FROM reports r JOIN companies co USING(uid) WHERE co.name='알파' AND version=2").fetchone()[0])
        self.assertTrue(any("sched.subscription" in d["field"] for d in rep), "보고서에 지난 버전 대비 변경 기록")
        ok_time = conn.execute("SELECT last_success FROM source_status WHERE source='ipostock'").fetchone()[0]

        # 3주차: IPOSTOCK 다운 → 재시도 후 실패, 기존 데이터 유지, 상태 기록
        self.net.down = {"ipostock.co.kr"}
        n_before = self.count("SELECT COUNT(*) FROM facts")
        r3 = self.run_at("2026-11-16T08:00:00+09:00")
        self.assertEqual(r3["status"], "partial")
        self.assertTrue(r3["stages"]["ipostock"]["degraded"])
        conn = connect(self.db)
        st = conn.execute("SELECT * FROM source_status WHERE source='ipostock'").fetchone()
        self.assertEqual(st["status"], "error")
        self.assertEqual(st["last_success"], ok_time, "마지막 성공 시각 보존")
        self.assertEqual(st["consecutive_failures"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM facts"), n_before, "실패해도 기존 데이터 유지")
        self.assertEqual(self.count("SELECT COUNT(*) FROM changes WHERE change_type='postponed' AND detected_at>='2026-11-16'"), 0,
                         "수집 실패를 '목록 이탈'로 오인하지 않음")

        # 4주차: 복구
        self.net.down = set()
        self.run_at("2026-11-23T08:00:00+09:00")
        st = connect(self.db).execute("SELECT * FROM source_status WHERE source='ipostock'").fetchone()
        self.assertEqual((st["status"], st["consecutive_failures"]), ("ok", 0))
        self.assertEqual(self.count("SELECT COUNT(*) FROM briefings"), 4, "주차별 브리핑 1건씩")

    def test_retry_with_backoff(self):
        calls = {"n": 0}

        def flaky(url, headers):
            if url.endswith("robots.txt"):
                return 404, b""
            calls["n"] += 1
            return (503, b"") if calls["n"] < 3 else (200, "ok".encode())
        waits = []
        http.set_transport(flaky, sleep=waits.append)
        self.assertEqual(http.fetch("https://x.example/a"), "ok")
        self.assertEqual(waits, [config.RETRY_BACKOFF_SEC, config.RETRY_BACKOFF_SEC * 2])

    def test_blocked_not_retried(self):
        n = {"c": 0}

        def deny(url, headers):
            n["c"] += 1
            return 403, b""
        http.set_transport(deny, sleep=lambda s: None)
        with self.assertRaises(http.FetchError) as e:
            http.fetch("https://y.example/a", check_robots=False)
        self.assertTrue(e.exception.blocked)
        self.assertEqual(n["c"], 1)


class TestInterest(Base):
    def _mk(self, conn, name, n7, p7, pubs=3):
        uid, _ = registry.resolve_company(conn, name)
        items = []
        for i in range(n7):
            items.append({"channel": "news", "url": f"https://n{i % pubs}.example/{name}/{i}", "title": f"{name} 공모 청약 기사 {i} 고유문장{i * 7}",
                          "snippet": f"고유 본문 {i} 내용 {i * 13} {name}", "published_at": "2026-11-01T09:00:00+09:00", "publisher": f"매체{i % pubs}"})
        for i in range(p7):
            items.append({"channel": "news", "url": f"https://n.example/{name}/p{i}", "title": f"{name} 상장 예정 기사 p{i} 문장{i * 11}",
                          "snippet": f"이전 본문 {i} {i * 17}", "published_at": "2026-10-24T09:00:00+09:00", "publisher": "매체0"})
        interest.process_mentions(conn, uid, name, items, None)
        return uid

    def test_missing_channels_not_zero(self):
        conn = connect(self.db)
        uid = self._mk(conn, "알파", 5, 1)
        now = datetime(2026, 11, 2, 8, tzinfo=KST)
        st = interest.window_stats(conn, uid, now, set(), name="알파")
        res = interest.score_group({uid: st})[uid]
        self.assertIsNone(res["score"])
        self.assertEqual(res["reliability"], "산출 불가")

    def test_small_sample_shrinkage_and_peer_norm(self):
        conn = connect(self.db)
        a = self._mk(conn, "알파", 3, 0)      # 0 → 3 (소표본 급증)
        b = self._mk(conn, "베타", 60, 20)    # 20 → 60
        now = datetime(2026, 11, 2, 8, tzinfo=KST)
        g = {u: interest.window_stats(conn, u, now, {"news"}, name=n) for u, n in ((a, "알파"), (b, "베타"))}
        res = interest.score_group(g)
        # 원시 증가율은 알파가 무한대지만 보정 후 베타보다 낮아야 한다
        self.assertLess(res[a]["components"]["growth"], res[b]["components"]["growth"])
        self.assertTrue(res[a]["small_sample"])
        self.assertIsNone(res[a]["components"]["engagement"], "참여도 데이터 없음 → 산출 불가")
        self.assertAlmostEqual(res[a]["weight_coverage"], 0.85)
        self.assertGreater(res[b]["score"], res[a]["score"])

    def test_relevance_and_dupes(self):
        conn = connect(self.db)
        uid, _ = registry.resolve_company(conn, "제논")
        items = [
            {"channel": "news", "url": "https://a.example/1", "title": "제논 수요예측 흥행", "snippet": "제논 공모 기관 경쟁률", "publisher": "A", "published_at": "2026-11-01"},
            {"channel": "news", "url": "https://b.example/1?utm_source=x", "title": "제논 수요예측 흥행", "snippet": "제논 공모 기관 경쟁률", "publisher": "B", "published_at": "2026-11-01"},
            {"channel": "news", "url": "https://c.example/1", "title": "제논 가스 가격 급등", "snippet": "반도체용 제논 가스", "publisher": "C", "published_at": "2026-11-01"},
            {"channel": "blog", "url": "https://d.example/1", "title": "제논 공모주 무료 리딩방", "snippet": "제논 청약 리딩방 오픈채팅", "publisher": "D", "published_at": "2026-11-01"},
        ]
        interest.process_mentions(conn, uid, "제논", items, ["제논 가스"])
        rows = {r["url"]: dict(r) for r in conn.execute("SELECT * FROM mentions")}
        self.assertEqual(rows["https://b.example/1?utm_source=x"]["is_duplicate"], 1)
        self.assertEqual(rows["https://c.example/1"]["relevant"], 0)
        self.assertEqual(rows["https://d.example/1"]["is_promo"], 1)
        interest.process_mentions(conn, uid, "제논", items, ["제논 가스"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0], 4, "재수집해도 중복 저장 없음")


def F(**kw):
    return {k.replace("__", "."): {"value": v} for k, v in kw.items()}


B = 1e8
PEERS = [{"name": f"P{i}", "origin": "analyst", "period": "2025", "fin_basis": "연결", "mktcap": m * B, "net_debt": 0,
          "revenue": 1000 * B, "ebitda": 100 * B, "net_income": n * B} for i, (m, n) in enumerate([(1000, 100), (1200, 100), (1500, 100), (900, -20)])]


class TestValuation(unittest.TestCase):
    base = dict(fin__basis="연결", offer__shares_post_ipo=10_000_000, offer__shares_new=2_000_000,
                offer__price_band={"low": 9000, "high": 11000}, valuation__peers=PEERS)

    def test_hold_when_missing(self):
        v = valuation.evaluate(F(offer__price_band={"low": 1, "high": 2}))
        self.assertEqual(v["verdict"], "판단 유보")
        self.assertTrue(any("재무" in r for r in v["reasons"]))

    def test_no_per_for_loss_and_ev_uses_proceeds(self):
        f = F(**self.base, fin__annual={"2025": {"revenue": 500 * B, "net_income": -10 * B, "ebitda": -5 * B,
                                                  "cash": 50 * B, "debt": 30 * B, "equity": 100 * B}})
        v = valuation.evaluate(f, "바이오")
        self.assertFalse(v["methods"]["PER"]["used"])
        self.assertIn("PER 미적용", v["methods"]["PER"]["why"])
        self.assertTrue(v["methods"]["EV/Sales"]["used"])
        # 공모 후 순차입금 = 30억 − (50억 + 200만주 × 10,000원)
        self.assertAlmostEqual(v["net_debt_post"], 30 * B - (50 * B + 2_000_000 * 10_000))
        # 음수 PER 비교기업 P3는 PER에서 제외되어야 함(여기선 PER 미사용이지만 EV/Sales엔 포함)
        self.assertEqual(len(v["methods"]["EV/Sales"]["peers"]), 4)

    def test_negative_peer_excluded_and_min_peers(self):
        f = F(**self.base, fin__annual={"2025": {"revenue": 500 * B, "net_income": 50 * B, "ebitda": 80 * B,
                                                  "cash": 50 * B, "debt": 30 * B, "equity": 100 * B}})
        v = valuation.evaluate(f)
        per = v["methods"]["PER"]
        self.assertTrue(per["used"])
        self.assertEqual({p["name"] for p in per["peers"]}, {"P0", "P1", "P2"})
        self.assertTrue(any(e["peer"] == "P3" and e["method"] == "PER" for e in v["excluded_peers"]))
        f2 = F(**{**self.base, "valuation__peers": PEERS[:2]}, fin__annual=f["fin.annual"]["value"])
        self.assertEqual(valuation.evaluate(f2)["verdict"], "판단 유보")

    def test_verdict_rules(self):
        fa = {"2025": {"revenue": 500 * B, "net_income": 50 * B, "ebitda": 80 * B, "cash": 50 * B, "debt": 30 * B, "equity": 100 * B}}
        cheap = valuation.evaluate(F(**{**self.base, "offer__price_band": {"low": 1000, "high": 1000}}, fin__annual=fa))
        dear = valuation.evaluate(F(**{**self.base, "offer__price_band": {"low": 900000, "high": 900000}}, fin__annual=fa))
        self.assertEqual(cheap["verdict"], "상대적 저평가 가능성")
        self.assertEqual(dear["verdict"], "상대적 고평가 가능성")

    def test_period_and_basis_mismatch_excluded(self):
        peers = [dict(p, period="2024") for p in PEERS[:3]] + [dict(PEERS[0], name="X", fin_basis="별도")]
        f = F(**{**self.base, "valuation__peers": peers}, fin__annual={"2025": {"revenue": 1, "net_income": 1, "ebitda": 1, "cash": 0, "debt": 0}})
        v = valuation.evaluate(f)
        self.assertEqual(v["verdict"], "판단 유보")
        whys = " ".join(e["why"] for e in v["excluded_peers"])
        self.assertIn("재무기간 불일치", whys)
        self.assertIn("연결/별도 불일치", whys)


class TestDart(Base):
    def test_list_limits_period_and_skips_listed_companies(self):
        from urllib.parse import parse_qs, urlparse

        from ipo_radar.collectors import dart
        seen = {}

        def t(url, headers, data=None):
            q = parse_qs(urlparse(url).query)
            seen.update(bgn=q["bgn_de"][0], end=q["end_de"][0])
            body = {"status": "000", "total_page": 1, "list": [
                {"corp_name": "비상장A", "corp_code": "1", "corp_cls": "E", "report_nm": "증권신고서(지분증권)", "rcept_no": "1"},
                {"corp_name": "코스닥B", "corp_code": "2", "corp_cls": "K", "report_nm": "증권신고서(지분증권)", "rcept_no": "2"},
                {"corp_name": "코넥스C", "corp_code": "3", "corp_cls": "N", "report_nm": "[기재정정]증권신고서(지분증권)", "rcept_no": "3"},
                {"corp_name": "비상장D", "corp_code": "4", "corp_cls": "E", "report_nm": "증권신고서(채무증권)", "rcept_no": "4"}]}
            return 200, json.dumps(body).encode()
        http.set_transport(t, sleep=lambda s: None)
        old = config.DART_API_KEY
        config.DART_API_KEY = "k"
        try:
            rows = dart.list_equity_filings(date(2026, 10, 30), lookback_days=120)
        finally:
            config.DART_API_KEY = old
        self.assertEqual((seen["bgn"], seen["end"]), ("20260802", "20261030"))  # 89일
        self.assertEqual([r["corp_name"] for r in rows], ["비상장A", "코넥스C"])
        self.assertTrue(rows[1]["is_amendment"])

    def test_equity_offering_reads_share_count(self):
        from ipo_radar.collectors import dart
        body = {"status": "000", "group": [
            {"title": "일반사항", "list": [{"sbd": "2026년 11월 05일 ~ 2026년 11월 06일", "pymd": "2026년 11월 10일"}]},
            {"title": "증권의종류", "list": [{"stksen": "기명식보통주", "stkcnt": "2,200,000", "slprc": "12,000"}]},
            {"title": "인수인정보", "list": [{"actsen": "대표", "actnmn": "한국투자증권", "udtcnt": "2,200,000"}]},
            {"title": "자금의사용목적", "list": [{"se": "시설자금", "amt": "14,180,000,000"}]}]}
        http.set_transport(lambda u, h, d=None: (200, json.dumps(body).encode()), sleep=lambda s: None)
        old = config.DART_API_KEY
        config.DART_API_KEY = "k"
        try:
            f = dart.equity_offering("3", date(2026, 10, 30))
        finally:
            config.DART_API_KEY = old
        self.assertEqual(f["offer.shares_total"], 2_200_000)
        self.assertEqual(f["sched.subscription"], {"start": "2026-11-05", "end": "2026-11-06"})
        self.assertEqual(f["offer.price_filing"], 12000)
        self.assertEqual(f["_underwriters"], ["한국투자증권"])


class TestUnderwriterCheck(unittest.TestCase):
    def test_actual_basis_verdicts(self):
        base = {"method": "PER", "multiple": 22.91, "earnings_basis": "actual", "per_share_value": 32783}
        cases = [(20000, "상대적 저평가 가능성"), (25000, "적정 범위"), (30000, "상대적 고평가 가능성")]
        for price, want in cases:
            v = valuation.underwriter_check(F(valuation__underwriter=base, offer__price_final=price))
            self.assertEqual(v["verdict"], want, price)
        v = valuation.underwriter_check(F(valuation__underwriter=base, offer__price_band={"low": 23000, "high": 26500}))
        self.assertAlmostEqual(v["eff_discount"], 1 - 24750 / 32783)
        self.assertAlmostEqual(v["fair_range"]["low"], 32783 * 0.70)
        self.assertEqual(v["forecast_dependency"], 0.0)

    def test_forecast_basis_is_held_with_achievement_ratio(self):
        u = {"method": "PER", "multiple": 19.15, "earnings_basis": "forecast", "per_share_value": 17760}
        v = valuation.underwriter_check(F(valuation__underwriter=u, offer__price_band={"low": 10500, "high": 13000}))
        self.assertEqual(v["verdict"], "판단 유보")
        self.assertEqual(v["forecast_dependency"], 1.0)
        self.assertAlmostEqual(v["by_price_u"][0]["required_achievement"], 10500 / 17760)
        self.assertIn("reference", v)

    def test_adjusted_earnings_recomputed(self):
        u = {"method": "PER", "multiple": 40.96, "earnings_basis": "adjusted", "value_total": 2045.73e8, "actual_value_total": 1206e8}
        v = valuation.underwriter_check(F(valuation__underwriter=u, offer__shares_post_ipo=14_330_000,
                                          offer__price_band={"low": 9700, "high": 12200}))
        self.assertAlmostEqual(v["actual_value"], 1206e8 / 14_330_000)
        self.assertEqual(v["verdict"], "상대적 고평가 가능성")
        self.assertAlmostEqual(v["forecast_dependency"], 1 - 1206 / 2045.73)

    def test_unknown_basis_is_provisional(self):
        u = {"method": "PER", "multiple": 22.91, "earnings_basis": "unknown", "per_share_value": 15254}
        v = valuation.underwriter_check(F(valuation__underwriter=u, offer__price_band={"low": 9400, "high": 11000}))
        self.assertTrue(v["provisional"])
        self.assertEqual(v["verdict"], "상대적 저평가 가능성")


class TestDemand(unittest.TestCase):
    def test_score_and_grade(self):
        from ipo_radar import demand
        v = demand.demand_view(F(demand__competition_ratio=1000.1, demand__above_top_ratio=0.989,
                                 offer__price_band={"low": 14000, "high": 17000}, offer__price_final=17000))
        self.assertEqual(v["grade"], "강함")
        self.assertAlmostEqual(v["coverage"], 0.7)
        self.assertEqual(demand.demand_view(F(offer__price_band={"low": 1, "high": 2}))["grade"], "발표 전")
        weak = demand.demand_view(F(demand__competition_ratio=30, demand__lockup_commit_ratio=0.01, offer__price_position="low"))
        self.assertEqual(weak["grade"], "약함")
        self.assertTrue(any("하단" in f for f in weak["flags"]))

    def test_aspects(self):
        self.assertEqual(interest.aspects_of("공모가 고평가 논란, 의무보유확약 낮아 오버행 우려"),
                         ["공모가·밸류", "수요예측·청약", "수급·물량"])

    def test_post_json_uses_body(self):
        seen = {}

        def t(url, headers, data=None):
            seen["data"] = data
            seen["ct"] = headers.get("Content-Type")
            return 200, b'{"ok": 1}'
        http.set_transport(t, sleep=lambda s: None)
        try:
            self.assertEqual(http.post_json("https://api.example/x", {"a": 1}, check_robots=False), {"ok": 1})
            self.assertEqual(seen["ct"], "application/json")
            self.assertEqual(json.loads(seen["data"]), {"a": 1})
        finally:
            http.set_transport(None)


class TestServer(unittest.TestCase):
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer

        from ipo_radar import server
        self.server_mod = server
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "index.html").write_text("<p>admin-dash</p>", encoding="utf-8")
        (self.tmp / "index_view.html").write_text("<p>viewer-dash</p>", encoding="utf-8")
        meta = {"meta": {"generated_at": "2026-10-30T00:00:00+09:00"}}
        (self.tmp / "data.json").write_text(json.dumps({**meta, "sources": [{"source": "dart", "last_error": "secret-err"}]}), encoding="utf-8")
        (self.tmp / "data_view.json").write_text(json.dumps({**meta, "sources": [{"source": "dart"}]}), encoding="utf-8")
        codes = [{"role": "admin", "secret": "admin-code-1"}, {"role": "viewer", "secret": server.hash_password("team-0915")}]
        from ipo_radar import db as db_mod, macro
        self.addCleanup(setattr, db_mod, "DB_PATH", db_mod.DB_PATH)
        db_mod.DB_PATH = self.tmp / "srv.sqlite3"  # 실제 DB를 건드리지 않음
        self.addCleanup(setattr, macro, "refresh_async", macro.refresh_async)
        self.macro_kicks = []
        macro.refresh_async = lambda trigger="manual": self.macro_kicks.append(trigger) or True  # 네트워크 수집 안 함
        self.app = server.build_app(out_dir=self.tmp, codes=codes, anon=False, with_sample=False, trust_proxy=False)
        self.app["index"] = "index.html"
        self.app["runner"].run_async = lambda trigger: True  # 실제 수집은 하지 않음
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, path, method="GET", data=None, cookie=None, headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = cookie
        body = None
        if data is not None:
            from urllib.parse import urlencode
            body = urlencode(data)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        out = (r.status, r.read(), {k.lower(): v for k, v in r.getheaders()})
        conn.close()
        return out

    def login(self, code, mode=None):
        mode = mode or ("admin" if code.startswith("admin") else "viewer")
        code_, _, h = self.req("/login", "POST", {"code": code, "mode": mode})
        ck = h.get("set-cookie", "")
        return code_, h.get("location"), ck.split(";")[0] if ck else None

    def test_redirect_to_login_and_login_page(self):
        self.assertEqual(self.req("/healthz")[0], 200)
        code, _, h = self.req("/")
        self.assertEqual((code, h.get("location")), (303, "/login"))
        self.assertNotIn("www-authenticate", h)  # 브라우저 기본 로그인 팝업을 띄우지 않음
        code, body, _ = self.req("/login")
        self.assertEqual(code, 200)
        self.assertIn("이용자 코드", body.decode())
        self.assertEqual(self.req("/api/status")[0], 401)

    def test_codes_select_mode_and_viewer_gets_stripped_files(self):
        code, loc, ck = self.login("team-0915")
        self.assertEqual((code, loc), (303, "/"))
        self.assertIn("HttpOnly", self.req("/login", "POST", {"code": "team-0915", "mode": "viewer"})[2]["set-cookie"])
        self.assertEqual(self.req("/ipo", cookie=ck)[1], b"<p>viewer-dash</p>")
        self.assertNotIn(b"secret-err", self.req("/data.json", cookie=ck)[1])
        st = json.loads(self.req("/api/status", cookie=ck)[1])
        self.assertEqual(st["role"], "viewer")
        self.assertNotIn("last_error", st)
        _, _, ack = self.login("admin-code-1")
        self.assertEqual(self.req("/ipo", cookie=ack)[1], b"<p>admin-dash</p>")
        self.assertIn(b"secret-err", self.req("/data.json", cookie=ack)[1])
        self.assertEqual(json.loads(self.req("/api/status", cookie=ack)[1])["role"], "admin")

    def test_wrong_code_tampered_and_expired_cookie(self):
        code, loc, ck = self.login("nope")
        self.assertEqual((code, loc, ck), (303, "/login?mode=viewer&e=1", None))
        # 모드가 맞지 않는 코드는 받지 않음
        self.assertEqual(self.login("admin-code-1", mode="viewer")[1], "/login?mode=viewer&e=1")
        self.assertEqual(self.login("team-0915", mode="admin")[1], "/login?mode=admin&e=1")
        body = self.req("/login?mode=admin")[1].decode()
        self.assertIn("관리자 모드로 입장", body)
        self.assertIn('name="mode" value="admin"', body)
        _, _, vck = self.login("team-0915")
        name, val = vck.split("=", 1)
        payload, sig = val.split(".")
        forged = self.server_mod._b64(b"admin|9999999999") + "." + sig  # 역할을 바꿔 위조
        self.assertEqual(self.req("/", cookie=f"{name}={forged}")[0], 303)
        old = self.server_mod.make_token(self.app, "admin", now=time_mod().time() - 13 * 3600)
        self.assertEqual(self.req("/", cookie=f"{name}={old}")[0], 303)
        self.assertEqual(self.req("/logout")[2]["location"], "/login")

    def test_refresh_requires_admin_and_header(self):
        H = {"X-Requested-With": "ipo-radar"}
        _, _, vck = self.login("team-0915")
        _, _, ack = self.login("admin-code-1")
        self.assertEqual(self.req("/admin/refresh", "POST", cookie=vck, headers=H)[0], 403)
        self.assertEqual(self.req("/admin/refresh", "POST", cookie=ack)[0], 400)
        self.assertEqual(self.req("/admin/refresh", "POST", cookie=ack, headers=H)[0], 202)

    def test_preparing_page_shows_progress_and_error(self):
        (self.tmp / "index.html").unlink()
        from ipo_radar import pipeline
        _, _, ack = self.login("admin-code-1")
        self.app["runner"].state.update(running=True)
        pipeline.PROGRESS.update(stage="dart", since="2026-10-30T09:00:00+09:00", run_id="x")
        code, body, _ = self.req("/ipo", cookie=ack)
        self.assertEqual(code, 503)
        self.assertIn("DART 공시 확인", body.decode())
        self.app["runner"].state.update(running=False, last_error="RuntimeError: boom")
        pipeline.PROGRESS.update(stage=None)
        self.assertIn("boom", self.req("/ipo", cookie=ack)[1].decode())

    def test_hub_menu_and_macro_routes(self):
        _, _, vck = self.login("team-0915")
        _, _, ack = self.login("admin-code-1")
        hub = self.req("/", cookie=vck)[1].decode()
        self.assertIn('href=\'/ipo\'', hub)
        self.assertIn('href=\'/macro\'', hub)
        self.assertNotIn("데이터 관리 · 관리자", hub)          # 이용자 메뉴에는 관리 항목 없음
        self.assertIn("데이터 관리 · 관리자", self.req("/", cookie=ack)[1].decode())
        page = self.req("/macro", cookie=vck)[1].decode()
        self.assertIn('"role": "viewer"', page)
        self.assertNotIn("/*__MACRO_BOOT__*/", page)
        self.assertEqual(self.req("/macro")[0], 303)            # 로그인 전에는 접속 화면으로
        self.assertEqual(self.req("/api/macro")[0], 401)
        js = json.loads(self.req("/api/macro", cookie=vck)[1])
        self.assertIn("us.10y", js["series"])
        self.assertTrue(all("message" not in st for st in js["status"].values()))  # 이용자에게 오류 상세 숨김
        self.assertTrue(self.macro_kicks)                       # 자료가 없으면 백그라운드 갱신 요청
        self.assertIn("message", json.loads(self.req("/api/macro", cookie=ack)[1])["status"]["fred"])

    def test_macro_manual_input_admin_only(self):
        H = {"X-Requested-With": "ipo-radar", "Content-Type": "application/json"}
        _, _, vck = self.login("team-0915")
        _, _, ack = self.login("admin-code-1")
        body = json.dumps({"text": "cds.삼성전자 5Y,2026-10-28,31\ncds.삼성전자 5Y,2026-10-29,32\nbad line"})
        import http.client
        def post(path, payload, cookie, headers=H):
            c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
            c.request("POST", path, body=payload.encode("utf-8"), headers={**headers, "Cookie": cookie})
            r = c.getresponse()
            out = (r.status, r.read())
            c.close()
            return out
        self.assertEqual(post("/admin/macro-manual", body, vck)[0], 403)
        self.assertEqual(post("/admin/macro-manual", body, ack, {"Content-Type": "application/json"})[0], 400)
        code, raw = post("/admin/macro-manual", body, ack)
        res = json.loads(raw)
        self.assertEqual((code, res["saved"], len(res["errors"])), (200, 2, 1))
        s = json.loads(self.req("/api/macro", cookie=vck)[1])["series"]["cds.삼성전자 5Y"]
        self.assertEqual((s["group"], s["last"]), ("cds", ["2026-10-29", 32.0]))
        code, raw = post("/admin/macro-manual", json.dumps({"delete": "cds.삼성전자 5Y"}), ack)
        self.assertEqual(json.loads(raw)["deleted"], 2)
        code, raw = post("/admin/macro-manual", json.dumps({"delete": "us.10y"}), ack)  # 자동 수집 시리즈는 못 지움
        self.assertEqual(code, 400)
        self.assertEqual(post("/admin/macro-refresh", "{}", ack)[0], 202)

    def test_codes_from_env_and_validation(self):
        import os
        server = self.server_mod
        keys = ("IPO_RADAR_ADMIN_CODE", "IPO_RADAR_USER_CODE")
        saved = {k: os.environ.get(k) for k in keys}
        old_pw, old_users = config.SERVER_ADMIN_PASSWORD, config.SERVER_USERS
        try:
            config.SERVER_USERS = ""
            os.environ["IPO_RADAR_ADMIN_CODE"], os.environ["IPO_RADAR_USER_CODE"] = "boss-code-77", "team77"
            roles = {c["role"] for c in server._codes_from_env()}
            self.assertEqual(roles, {"admin", "viewer"})
            os.environ.pop("IPO_RADAR_ADMIN_CODE")
            config.SERVER_ADMIN_PASSWORD = "legacy-admin-pw"  # 예전 관리자 비밀번호는 관리자 코드로 인정
            self.assertIn({"role": "admin", "secret": "legacy-admin-pw"}, server._codes_from_env())
            with self.assertRaises(SystemExit):  # 너무 짧은 코드
                server.build_app(out_dir=self.tmp, codes=[{"role": "admin", "secret": "short"}], anon=False)
            with self.assertRaises(SystemExit):  # 관리자·이용자 코드가 같음
                server.build_app(out_dir=self.tmp, codes=[{"role": "admin", "secret": "same-code-1"},
                                                          {"role": "viewer", "secret": "same-code-1"}], anon=False)
        finally:
            config.SERVER_ADMIN_PASSWORD, config.SERVER_USERS = old_pw, old_users
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_admin_changes_user_code(self):
        import os
        old_db = config.DB_PATH
        config.DB_PATH = self.tmp / "db.sqlite3"  # access.json을 임시 폴더에 저장
        for k in ("RENDER_API_KEY", "RENDER_SERVICE_ID"):
            os.environ.pop(k, None)
        try:
            H = {"X-Requested-With": "ipo-radar", "Content-Type": "application/json"}
            _, _, vck = self.login("team-0915")
            _, _, ack = self.login("admin-code-1")

            def post(body, ck):
                import http.client
                c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
                c.request("POST", "/admin/user-code", body=json.dumps(body), headers={**H, "Cookie": ck})
                r = c.getresponse()
                raw = r.read()
                try:
                    out = (r.status, json.loads(raw or b"{}"))
                except ValueError:
                    out = (r.status, {"text": raw.decode()})
                c.close()
                return out
            self.assertEqual(post({"code": "newteam1"}, vck)[0], 403)            # 이용자는 못 바꿈
            self.assertEqual(post({"code": "abc"}, ack)[0], 400)                 # 너무 짧음
            self.assertEqual(post({"code": "admin-code-1"}, ack)[0], 400)        # 관리자 코드와 같음
            code, res = post({"code": "newteam1"}, ack)
            self.assertEqual((code, res["ok"], res["access"]["source"]), (200, True, "dashboard"))
            self.assertIsNone(res["render_synced"])                              # Render 키 없으면 동기화 안 함
            self.assertEqual(self.req("/", cookie=vck)[0], 303)                  # 기존 이용자 세션 끊김
            self.assertEqual(self.req("/", cookie=ack)[0], 200)                  # 관리자 세션 유지
            self.assertEqual(self.login("team-0915")[1], "/login?mode=viewer&e=1")
            self.assertEqual(self.login("newteam1")[1], "/")
            saved = json.loads((self.tmp / "access.json").read_text())
            self.assertNotIn("newteam1", json.dumps(saved))                      # 원문이 아니라 해시로 저장
            # 재시작 시 저장된 코드를 다시 읽음
            old_users, old_pw = config.SERVER_USERS, config.SERVER_ADMIN_PASSWORD
            config.SERVER_USERS, config.SERVER_ADMIN_PASSWORD = "", "admin-code-1"
            try:
                codes = self.server_mod._codes_from_env()
                self.assertTrue(self.server_mod.check_code({"codes": codes}, "newteam1", "viewer"))
            finally:
                config.SERVER_USERS, config.SERVER_ADMIN_PASSWORD = old_users, old_pw
            code, res = post({"disable": True}, ack)
            self.assertEqual((code, res["access"]["user_code_set"]), (200, False))
            self.assertEqual(self.login("newteam1")[1], "/login?mode=viewer&e=1")
        finally:
            config.DB_PATH = old_db

    def test_render_sync_called_when_configured(self):
        import os
        old_db = config.DB_PATH
        config.DB_PATH = self.tmp / "db.sqlite3"
        calls = []
        try:
            res = self.server_mod.set_user_code(self.app, "teamcode9", sync=lambda v: (calls.append(v) or (True, "ok")))
            self.assertEqual((res["ok"], res["render_synced"], calls), (True, True, ["teamcode9"]))
            os.environ["RENDER_API_KEY"], os.environ["RENDER_SERVICE_ID"] = "k", "srv-x"
            self.assertTrue(self.server_mod.access_info(self.app)["render_sync"])
        finally:
            config.DB_PATH = old_db
            os.environ.pop("RENDER_API_KEY", None)
            os.environ.pop("RENDER_SERVICE_ID", None)

    def test_lockout(self):
        for _ in range(10):
            self.login("bad-code")
        code, body, _ = self.req("/login", "POST", {"code": "admin-code-1", "mode": "admin"})
        self.assertEqual(code, 429)


def time_mod():
    import time
    return time


class FakeBackend:
    """AI 백엔드 대역: generate 호출을 기록하고 스키마에 맞는 JSON을 돌려준다."""

    def __init__(self, fail_names=(), model="fake-model"):
        self.calls, self.fail_names, self.model = [], set(fail_names), model

    def generate(self, system, text, schema):
        self.calls.append({"system": system, "text": text, "schema": schema})
        if any(n in text for n in self.fail_names):
            raise RuntimeError("boom")
        if "one_liner" in schema["properties"]:
            body = {"one_liner": "요약", "business": "사업", "watch_points": [{"text": "a", "evidence": ["fin.annual"]}],
                    "risks": [], "valuation_view": {"text": "판단 유보", "evidence": ["valuation"]},
                    "talking_points": [], "data_gaps": ["재무"]}
        else:
            body = {"headline": "h", "summary": "s", "highlights": [], "cautions": []}
        return body, self.model


class TestAI(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = connect(self.tmp / "ai.sqlite3")
        self.run = type("Run", (), {"at": "2026-10-30T08:00:00+09:00"})()
        self.content = {"company": {"name": "A사", "sector": "x", "market": None, "underwriters": None, "status": "active"},
                        "stage": "subscription", "facts": {"fin.annual": {"value": {"2025": {"revenue": 1}}, "basis": "news_reported",
                                                                         "source": {"publisher": "P"}, "collected_at": "t1"}},
                        "valuation": {"verdict": "판단 유보"}, "demand": {}, "summary": {"points": [], "risks": []}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _with(self, backend):
        from ipo_radar import ai
        orig = ai.make_client
        ai.make_client = lambda: backend
        self.addCleanup(setattr, ai, "make_client", orig)
        return ai

    def test_generate_then_reuse(self):
        fake = FakeBackend()
        ai = self._with(fake)
        r1 = ai.refresh(self.conn, self.run, {"u1": self.content}, {"week_of": "2026-10-26", "picks": []})
        self.assertEqual((r1["generated"], r1["errors"]), (2, []))
        changed = json.loads(json.dumps(self.content))
        changed["facts"]["fin.annual"]["collected_at"] = "t2"  # 수집 시각만 바뀌면 재생성하지 않음
        r2 = ai.refresh(self.conn, self.run, {"u1": changed}, {"week_of": "2026-10-26", "picks": []})
        self.assertEqual((r2["generated"], r2["skipped"]), (0, 1))
        self.assertEqual(len(fake.calls), 2)
        note = ai.load_notes(self.conn)["u1"]
        self.assertEqual((note["one_liner"], note["_model"]), ("요약", "fake-model"))

    def test_parallel_partial_failure(self):
        ai = self._with(FakeBackend(fail_names={"B사"}))
        b = json.loads(json.dumps(self.content))
        b["company"]["name"] = "B사"
        r = ai.refresh(self.conn, self.run, {"u1": self.content, "u2": b}, None)
        self.assertEqual((r["generated"], len(r["errors"])), (1, 1))
        self.assertIn("B사", r["errors"][0])

    def test_gemini_backend_picks_model_and_strips_schema(self):
        from ipo_radar import ai
        sent = {}

        class Models:
            def list(self):
                mk = lambda n, acts: type("M", (), {"name": f"models/{n}", "supported_actions": acts})()  # noqa: E731
                return [mk("gemini-3.8-flash", ["generateContent"]), mk("gemini-3.5-flash-lite", ["generateContent"]),
                        mk("text-embedding-004", ["embedContent"])]

            def generate_content(self, model, contents, config):
                sent.update(model=model, contents=contents, config=config)
                return type("R", (), {"text": json.dumps({"headline": "h", "summary": "s", "highlights": [], "cautions": []})})()

        be = ai.GeminiBackend(type("C", (), {"models": Models()})(), "auto")
        self.assertEqual(be.model, "gemini-3.5-flash-lite")  # 무료 한도가 큰 Flash-Lite 우선
        out, model = be.generate("sys", "본문", ai.WEEKLY_SCHEMA)
        self.assertEqual((out["headline"], model), ("h", "gemini-3.5-flash-lite"))
        cfg = sent["config"]
        self.assertEqual(cfg.response_mime_type, "application/json")
        self.assertEqual(cfg.system_instruction, "sys")
        self.assertNotIn("additionalProperties", json.dumps(cfg.response_json_schema))
        self.assertEqual(ai.GeminiBackend(type("C", (), {"models": Models()})(), "gemini-3.8-flash").model, "gemini-3.8-flash")

    def test_gemini_retries_on_429_then_reports_quota(self):
        from google.genai import errors

        from ipo_radar import ai
        calls = {"n": 0}

        class Models:
            def generate_content(self, model, contents, config):
                calls["n"] += 1
                raise errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
        waits = []
        be = ai.GeminiBackend(type("C", (), {"models": Models()})(), "gemini-x", sleep=waits.append)
        with self.assertRaises(RuntimeError) as cm:
            be.generate("s", "t", ai.WEEKLY_SCHEMA)
        self.assertIn("한도", str(cm.exception))
        self.assertEqual((calls["n"], waits), (4, [20, 40, 60]))

    def test_claude_backend_request_shape(self):
        from ipo_radar import ai
        seen = {}

        class M:
            def create(self, **kw):
                seen.update(kw)
                blk = type("B", (), {"type": "text", "text": json.dumps({"headline": "h", "summary": "s", "highlights": [], "cautions": []})})()
                return type("R", (), {"stop_reason": "end_turn", "content": [blk], "model": kw["model"]})()
        be = ai.ClaudeBackend(type("C", (), {"beta": type("B", (), {"messages": M()})()})(), "auto")
        out, model = be.generate("s", "t", ai.WEEKLY_SCHEMA)
        self.assertEqual((model, seen["fallbacks"]), ("claude-opus-5-5", "default"))
        be2 = ai.ClaudeBackend(be.client, "claude-haiku-5-5")
        be2.generate("s", "t", ai.WEEKLY_SCHEMA)
        self.assertEqual(seen["model"], "claude-haiku-5-5")

    def test_gemini_ignores_leftover_claude_model_name(self):
        import os

        from ipo_radar import ai
        picked = {}
        orig_model, orig_cls = ai.MODEL, ai.GeminiBackend
        ai.MODEL = "claude-opus-5-5"
        ai.GeminiBackend = lambda client, model: picked.setdefault("model", model)
        old = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "test-key"
        try:
            ai.make_client()
            self.assertEqual(picked["model"], "auto")
        finally:
            ai.MODEL, ai.GeminiBackend = orig_model, orig_cls
            if old is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = old

    def test_catchup_records_ai_status(self):
        import os

        from ipo_radar import pipeline
        saved = {k: os.environ.pop(k, None) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY")}
        try:
            r = type("R", (), {"trigger": "catch-up", "conn": self.conn, "at": "2026-10-30T08:00:00+09:00"})()
            pipeline.run_ai(r)
            row = self.conn.execute("SELECT status, last_error FROM source_status WHERE source='ai_api'").fetchone()
            self.assertEqual(row["status"], "not_configured")
            self.assertIn("GEMINI_API_KEY", row["last_error"])
            os.environ["GEMINI_API_KEY"] = "x"
            pipeline.run_ai(r)
            row = self.conn.execute("SELECT status FROM source_status WHERE source='ai_api'").fetchone()
            self.assertEqual(row["status"], "disabled")
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_not_configured_without_key(self):
        import os

        from ipo_radar import ai
        saved = {k: os.environ.pop(k, None) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY")}
        try:
            with self.assertRaises(ai.NotConfigured):
                ai.make_client()
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class TestSchedule(unittest.TestCase):
    def test_next_run_monday_8_kst(self):
        self.assertEqual(next_run(datetime(2026, 10, 29, 21, tzinfo=KST)).isoformat(), "2026-11-02T08:00:00+09:00")
        self.assertEqual(next_run(datetime(2026, 11, 2, 7, 59, tzinfo=KST)).isoformat(), "2026-11-02T08:00:00+09:00")
        self.assertEqual(next_run(datetime(2026, 11, 2, 8, 0, tzinfo=KST)).isoformat(), "2026-11-09T08:00:00+09:00")
        # UTC 일요일 23:00 = KST 월요일 08:00 (GitHub Actions cron '0 23 * * 0')
        from datetime import timezone
        utc = datetime(2026, 11, 1, 22, 59, tzinfo=timezone.utc)
        self.assertEqual(next_run(utc).isoformat(), "2026-11-02T08:00:00+09:00")

    def test_recovery_retries_failed_run(self):
        seq = iter(["failed", "failed", "success"])
        waits = []
        res = run_with_recovery(run_fn=lambda **kw: {"status": next(seq), "run_id": kw["trigger"]}, sleep=waits.append, wait_sec=5)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["run_id"], "schedule-retry2")
        self.assertEqual(waits, [5, 5])


class TestMacro(unittest.TestCase):
    def setUp(self):
        import os
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = connect(self.tmp / "m.sqlite3")
        self._env = {k: os.environ.get(k) for k in ("FRED_API_KEY", "ECOS_API_KEY", "IPO_RADAR_AI_PRICE_IN", "IPO_RADAR_AI_PRICE_OUT")}

    def tearDown(self):
        import os
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        http.set_transport(None)
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_collect_parses_sources_and_masks_keys(self):
        import os
        from ipo_radar import macro
        os.environ["FRED_API_KEY"] = "fredsecret123"
        os.environ["ECOS_API_KEY"] = "ecossecret456"
        seen = []

        def net(url, headers, data=None):
            seen.append(url)
            if "stlouisfed" in url:
                if "series_id=VIXCLS" in url:
                    return 500, b"oops"
                return 200, json.dumps({"observations": [{"date": "2026-10-27", "value": "4.10"},
                                                         {"date": "2026-10-28", "value": "."},
                                                         {"date": "2026-10-29", "value": "4.15"}]}).encode()
            if "ecos.bok.or.kr" in url:
                if "010230000" in url:
                    return 200, json.dumps({"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}}).encode()
                return 200, json.dumps({"StatisticSearch": {"list_total_count": 2, "row": [
                    {"TIME": "20261028", "DATA_VALUE": "2.851"}, {"TIME": "20261029", "DATA_VALUE": "2,870"}]}}).encode()
            if "alternative.me" in url:
                if url.endswith("robots.txt"):
                    return 404, b""
                return 200, json.dumps({"data": [{"value": "40", "value_classification": "Fear", "timestamp": "1761696000"}]}).encode()
            return 404, b""
        http.set_transport(net, sleep=lambda s: None)
        res = macro.collect(self.conn, today=date(2026, 10, 30))
        self.assertEqual(res, {"fred": "partial", "ecos": "ok", "altme": "ok"})
        rows = self.conn.execute("SELECT date, value FROM macro_points WHERE series='us.10y' ORDER BY date").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("2026-10-27", 4.10), ("2026-10-29", 4.15)])  # "." 결측은 버림
        self.assertEqual(self.conn.execute("SELECT value FROM macro_points WHERE series='kr.3y' AND date='2026-10-29'").fetchone()[0], 2870.0)
        st = {r["source"]: dict(r) for r in self.conn.execute("SELECT * FROM macro_status")}
        self.assertIn("VIX", st["fred"]["message"])
        self.assertNotIn("fredsecret123", json.dumps(st))      # 키가 화면용 메시지에 새지 않음
        self.assertTrue(any("ecossecret456" in u for u in seen))
        # 두 번째 수집은 최근 45일만 다시 요청
        seen.clear()
        macro.collect(self.conn, today=date(2026, 10, 30))
        self.assertTrue(any("observation_start=2026-09-14" in u for u in seen if "DGS10" in u))

    def test_missing_keys_marked_not_configured(self):
        import os
        from ipo_radar import macro
        os.environ.pop("FRED_API_KEY", None)
        os.environ.pop("ECOS_API_KEY", None)
        http.set_transport(lambda u, h, d=None: (403, b""), sleep=lambda s: None)
        res = macro.collect(self.conn, today=date(2026, 10, 30))
        self.assertEqual((res["fred"], res["ecos"]), ("not_configured", "not_configured"))
        self.assertEqual(res["altme"], "blocked")
        out = macro.build(self.conn)
        self.assertEqual(out["series"]["us.10y"]["points"], [])
        self.assertFalse(out["sentiment"]["available"])

    def test_manual_parse_rejects_bad_rows(self):
        from ipo_radar import macro
        rows, errs = macro.parse_manual("시리즈,날짜,값\ncds.A,2026-10-29,32\ndram.DDR5,2026/10/29,6.1\nus.10y,2026-10-29,4\n"
                                        "pizza.지수,29-10-2026,3\ncds.B,2026-10-29,abc\n# 메모\ncds.C,2026-10-29")
        self.assertEqual(rows, [("cds.A", "2026-10-29", 32.0), ("dram.DDR5", "2026-10-29", 6.1)])
        self.assertEqual(len(errs), 4)

    def test_bond_price_and_moves(self):
        from ipo_radar import macro
        self.assertAlmostEqual(macro.par_bond_price(4.0, 4.0, 10), 100.0, places=6)
        self.assertLess(macro.par_bond_price(4.0, 4.5, 10), 100)
        self.assertLess(macro.par_bond_price(4.0, 4.5, 30), macro.par_bond_price(4.0, 4.5, 10))  # 장기채가 더 민감
        pts = [("2025-12-31", 4.0), ("2026-10-01", 4.2), ("2026-10-22", 4.3), ("2026-10-28", 4.4), ("2026-10-29", 4.5)]
        m = macro.bond_moves(pts, 10)
        self.assertEqual(m["moves"]["1d"]["bp"], 10.0)
        self.assertEqual(m["moves"]["1w"]["from"], "2026-10-22")
        self.assertEqual(m["moves"]["ytd"]["bp"], 50.0)
        self.assertTrue(-4.5 < m["moves"]["ytd"]["price_pct"] < -3.5)
        self.assertNotIn("1y", m["moves"])  # 1년 전 자료가 없으면 만들지 않음

    def test_sentiment_index_needs_two_components(self):
        from ipo_radar import macro
        from datetime import timedelta
        d0 = date(2025, 1, 1)
        days = [(d0 + timedelta(days=i)).isoformat() for i in range(300)]
        vix = [(d, 30 - i * 0.05) for i, d in enumerate(days)]       # 변동성 하락 → 탐욕
        hy = [(d, 5 - i * 0.005) for i, d in enumerate(days)]        # 스프레드 축소 → 탐욕
        self.assertFalse(macro.sentiment_index({"risk.vix": vix})["available"])
        out = macro.sentiment_index({"risk.vix": vix, "credit.hy": hy})
        self.assertTrue(out["available"])
        self.assertGreater(out["value"], 90)
        self.assertEqual(out["label"], "극단적 탐욕")
        self.assertEqual(set(out["components"]), {"vix", "hy"})

    def test_ai_usage_recorded_and_priced(self):
        import os
        from ipo_radar import ai, macro

        class UsageBackend(FakeBackend):
            def generate(self, system, text, schema):
                body, model = super().generate(system, text, schema)
                return {**body, "_usage": {"input": 1000, "output": 500, "total": 1500}}, model
        orig = ai.make_client
        ai.make_client = lambda: UsageBackend()
        self.addCleanup(setattr, ai, "make_client", orig)
        content = {"company": {"name": "A사", "sector": "x", "market": None, "underwriters": None, "status": "active"},
                   "stage": "subscription", "facts": {}, "valuation": {}, "demand": {}, "summary": {"points": [], "risks": []}}
        run = type("Run", (), {"at": "2026-10-30T08:00:00+09:00"})()
        ai.refresh(self.conn, run, {"u1": content}, {"week_of": "2026-10-26", "picks": []})
        self.assertNotIn("_usage", ai.load_notes(self.conn)["u1"])
        u = macro.usage_summary(self.conn)
        self.assertEqual((u["total"]["calls"], u["total"]["input"], u["total"]["output"], u["cost_usd"]), (2, 2000, 1000, None))
        os.environ["IPO_RADAR_AI_PRICE_IN"], os.environ["IPO_RADAR_AI_PRICE_OUT"] = "0.5", "2"
        self.assertAlmostEqual(macro.usage_summary(self.conn)["cost_usd"], 0.003)


if __name__ == "__main__":
    unittest.main()
