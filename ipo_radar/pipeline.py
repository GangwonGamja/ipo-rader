"""주간 파이프라인.

일정 수집 → 기업 식별 → 공식 공시 확인 → 재무·뉴스·커뮤니티 갱신 → 점수·가치평가 재계산
→ 보고서 갱신 → 브리핑 저장 → 대시보드 데이터 반영

- 각 단계는 독립적으로 실패할 수 있고, 실패해도 다음 단계는 기존 데이터로 계속한다.
- 소스별 마지막 성공 시각·오류를 source_status에 남긴다.
- 모든 쓰기는 upsert / 변경 시에만 기록이라 같은 주에 여러 번 실행해도 중복이 생기지 않는다.
"""
from __future__ import annotations

import json
import time
import traceback
from datetime import date, datetime, timedelta

from . import config, demand, interest, registry, report, valuation
from .collectors import buzz, dart, datalab, ipostock
from .db import connect, dumps, iso, loads, set_source_status, upsert_source
from .http import FetchError, reset_run_state


def _today(now: datetime) -> date:
    return now.astimezone(config.KST).date()


class Run:
    def __init__(self, conn, now: datetime, trigger: str):
        self.conn, self.now, self.trigger = conn, now, trigger
        self.at = iso(now)
        self.today = _today(now)
        self.run_id = now.astimezone(config.KST).strftime("%Y%m%dT%H%M%S") + "-" + trigger
        self.stages: dict[str, dict] = {}
        self._t0 = time.time()
        self.channels_ok: dict[str, set] = {}
        conn.execute("INSERT INTO runs(run_id,trigger,started_at,status) VALUES(?,?,?,?)",
                     (self.run_id, trigger, self.at, "running"))
        conn.commit()

    def stage(self, name, fn, *a, **kw):
        t0 = time.time()
        try:
            res = fn(*a, **kw)
            st = {"status": "ok", **(res or {})}
        except Exception as e:  # 단계 실패는 기록 후 계속
            self.conn.rollback()
            st = {"status": "error", "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=3)}
        st["seconds"] = round(time.time() - t0, 2)
        self.stages[name] = st
        self.conn.commit()
        return st

    def finish(self):
        bad = [k for k, v in self.stages.items() if v["status"] == "error"]
        degraded = [k for k, v in self.stages.items() if v.get("degraded")]
        status = "failed" if len(bad) == len(self.stages) else ("partial" if bad or degraded else "success")
        self.conn.execute("UPDATE runs SET finished_at=?, status=?, stages=? WHERE run_id=?",
                          (iso(self.now + timedelta(seconds=time.time() - self._t0)), status, dumps(self.stages), self.run_id))
        self.conn.commit()
        return status


# ---------- 1. 시드(웹검색 결과·수동 입력) ----------

def load_seed_files(r: Run):
    n_files = n_facts = 0
    files = sorted(config.SEED_DIR.glob("*.json")) + sorted(config.MANUAL_DIR.glob("*.json"))
    for path in files:
        d = json.loads(path.read_text(encoding="utf-8"))
        stype = d.get("source_type", "manual")
        srcs = {}
        for key, s in d.get("sources", {}).items():
            srcs[key] = upsert_source(r.conn, s["url"], s.get("type") if s.get("type") in registry.SOURCE_RANK else stype,
                                      s.get("title"), s.get("publisher"), s.get("as_of"), d.get("retrieved_at"))
        for c in d.get("companies", []):
            first_src = srcs.get(c.get("src_for_discovery")) or (srcs.get(c["facts"][0]["src"]) if c.get("facts") else None)
            uid, _ = registry.resolve_company(
                r.conn, c["name"], corp_code=c.get("corp_code"), run_id=r.run_id, source_id=first_src,
                source_type=stype, at=r.at, market=c.get("market"), sector=c.get("sector"),
                underwriters=c.get("underwriters"), exclude_terms=c.get("exclude_terms"))
            if c.get("status"):
                registry.set_status(r.conn, uid, c["status"], run_id=r.run_id, source_id=first_src,
                                    note=c.get("note"), at=r.at)
            if c.get("note"):
                registry.set_fact(r.conn, uid, "note.discovery", c["note"], source_id=first_src, run_id=r.run_id,
                                  basis="news_reported", at=r.at)
            for f in c.get("facts", []):
                sid = srcs.get(f.get("src"))
                src_as_of = d["sources"].get(f.get("src"), {}).get("as_of") if f.get("src") else None
                registry.set_fact(r.conn, uid, f["key"], f["value"], source_id=sid, run_id=r.run_id,
                                  unit=f.get("unit"), basis=f.get("basis", "confirmed" if stype != "websearch_summary" else "news_reported"),
                                  as_of=f.get("as_of") or src_as_of, at=r.at)
                if f.get("note"):
                    registry.set_fact(r.conn, uid, f"note.{f['key']}", f["note"], source_id=sid, run_id=r.run_id,
                                      basis="news_reported", at=r.at)
                n_facts += 1
        n_files += 1
    return {"files": n_files, "facts": n_facts}


# ---------- 2. IPOSTOCK 일정 ----------

def collect_ipostock(r: Run):
    try:
        rows, urls = ipostock.collect(r.today)
    except FetchError as e:
        set_source_status(r.conn, "ipostock", False, str(e), status="blocked" if e.blocked else "error", at=r.at)
        r.conn.commit()
        return {"degraded": True, "error": str(e), "months": ipostock.months_to_scan(r.today)}
    seen = set()
    for row in rows:
        sid = upsert_source(r.conn, row["url"], "aggregator", "IPOSTOCK 공모 일정", "IPOSTOCK", r.today.isoformat(), r.at)
        uid, _ = registry.resolve_company(r.conn, row["name"], ext_ids={"ipostock": row.get("ext_id")},
                                          run_id=r.run_id, source_id=sid, source_type="aggregator", at=r.at)
        registry.set_fact(r.conn, uid, row["event"], {"start": row["start"], "end": row["end"]},
                          source_id=sid, run_id=r.run_id, as_of=r.today.isoformat(), at=r.at)
        seen.add(uid)
    # 직전에 IPOSTOCK에 있던 미래 일정 기업이 사라지면 '목록에서 사라짐'으로 표시(철회 확정은 공시로만)
    for row in r.conn.execute(
            """SELECT DISTINCT f.uid FROM facts f JOIN sources s ON s.source_id=f.source_id
               WHERE s.publisher='IPOSTOCK' AND f.key LIKE 'sched.%' AND json_extract(f.value,'$.start')>=?""",
            (r.today.isoformat(),)):
        if row["uid"] not in seen:
            registry._record_change(r.conn, r.run_id, row["uid"], "postponed", "ipostock_listing", "listed",
                                    "IPOSTOCK 달력에서 사라짐(연기·철회 가능성, 공시 확인 필요)", None, r.at, dedupe=True)
    set_source_status(r.conn, "ipostock", True, at=r.at, note=f"{len(urls)}개 월 조회")
    return {"rows": len(rows), "months": urls}


# ---------- 3. DART 공식 공시 ----------

def verify_dart(r: Run):
    try:
        filings = dart.list_equity_filings(r.today)
    except dart.NotConfigured as e:
        set_source_status(r.conn, "dart", None, str(e), status="not_configured", at=r.at)
        return {"degraded": True, "error": str(e)}
    except FetchError as e:
        set_source_status(r.conn, "dart", False, str(e), status="blocked" if e.blocked else "error", at=r.at)
        return {"degraded": True, "error": str(e)}
    n = 0
    for f in filings:
        sid = upsert_source(r.conn, f["url"], "official_disclosure", f.get("report_nm"), "DART",
                            f.get("rcept_dt"), r.at)
        uid, _ = registry.resolve_company(r.conn, f["corp_name"], corp_code=f["corp_code"], run_id=r.run_id,
                                          source_id=sid, source_type="official_disclosure", at=r.at,
                                          stock_code=f.get("stock_code") or None)
        if f["is_withdrawal"]:
            registry.set_status(r.conn, uid, "withdrawn", run_id=r.run_id, source_id=sid, at=r.at)
            continue
        if f["is_amendment"]:
            registry.set_status(r.conn, uid, "amendment", run_id=r.run_id, source_id=sid,
                                note=f"{f.get('report_nm')} ({f.get('rcept_dt')})", at=r.at)
        try:
            facts = dart.equity_offering(f["corp_code"], r.today)
        except FetchError:
            continue
        uw = facts.pop("_underwriters", None)
        if uw:
            registry.resolve_company(r.conn, f["corp_name"], corp_code=f["corp_code"], source_type="official_disclosure",
                                     at=r.at, underwriters=", ".join(uw))
        for k, v in facts.items():
            registry.set_fact(r.conn, uid, k, v, source_id=sid, run_id=r.run_id, as_of=f.get("rcept_dt"), at=r.at)
        n += 1
    set_source_status(r.conn, "dart", True, at=r.at, note=f"지분증권 공시 {len(filings)}건")
    return {"filings": len(filings), "parsed": n}


def refresh_financials(r: Run):
    if not config.DART_API_KEY:
        set_source_status(r.conn, "dart_financials", None, "DART_API_KEY 미설정", status="not_configured", at=r.at)
        return {"degraded": True, "error": "DART_API_KEY 미설정"}
    n = 0
    for c in r.conn.execute("SELECT uid, corp_code FROM companies WHERE corp_code IS NOT NULL AND is_sample=0 AND status='active'").fetchall():
        annual = {}
        for y in range(r.today.year - 3, r.today.year):
            d = dart.financials(c["corp_code"], y)
            if d:
                annual[str(y)] = d
        if annual:
            sid = upsert_source(r.conn, f"https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?corp_code={c['corp_code']}",
                                "official_disclosure", "DART 단일회사 재무제표", "DART", None, r.at)
            registry.set_fact(r.conn, c["uid"], "fin.annual", annual, source_id=sid, run_id=r.run_id, unit="KRW", at=r.at)
            n += 1
    set_source_status(r.conn, "dart_financials", True, at=r.at, note=f"{n}개 기업 재무 확보")
    return {"companies_with_fin": n}


# ---------- 4. 뉴스·커뮤니티 ----------

def refresh_buzz(r: Run):
    rows = r.conn.execute("SELECT uid, name, exclude_terms FROM companies WHERE is_sample=0 AND kind='general' AND status!='withdrawn'").fetchall()
    agg: dict[str, list] = {}
    for c in rows:
        items, status = buzz.collect_for(c["name"])
        for k, v in status.items():
            agg.setdefault(k, []).append(v)
        interest.process_mentions(r.conn, c["uid"], c["name"], items, loads(c["exclude_terms"]), at=r.at)
        ok = set()
        if status.get("naver_news") == "ok" or status.get("google_news") == "ok":
            ok.add("news")
        if status.get("naver_blog") == "ok":
            ok.add("blog")
        if status.get("naver_cafe") == "ok":
            ok.add("cafe")
        if status.get("youtube") == "ok":
            ok.add("video")
        r.channels_ok[c["uid"]] = ok
    for k, vs in agg.items():
        if all(v == "ok" for v in vs):
            set_source_status(r.conn, k, True, at=r.at)
        elif all(str(v).startswith("not_configured") for v in vs):
            set_source_status(r.conn, k, None, vs[0], status="not_configured", at=r.at)
        elif all(str(v).startswith(("n/a", "disabled")) for v in vs):
            set_source_status(r.conn, k, None, vs[0], status="disabled", at=r.at)
        else:
            err = next(v for v in vs if v != "ok")
            set_source_status(r.conn, k, False, err, status="blocked" if "접근 거부" in err or "robots" in err else "error", at=r.at)
    degraded = not any(r.channels_ok.values())
    return {"companies": len(rows), "degraded": degraded}


def refresh_search(r: Run):
    rows = r.conn.execute("SELECT uid, name FROM companies WHERE is_sample=0 AND kind='general' AND status!='withdrawn'").fetchall()
    try:
        res = datalab.trends([c["name"] for c in rows], r.today)
    except datalab.NotConfigured as e:
        set_source_status(r.conn, "naver_datalab", None, str(e), status="not_configured", at=r.at)
        return {"degraded": True, "error": str(e)}
    except FetchError as e:
        set_source_status(r.conn, "naver_datalab", False, str(e), status="blocked" if e.blocked else "error", at=r.at)
        return {"degraded": True, "error": str(e)}
    n = 0
    for c in rows:
        for day, v in res.get(c["name"], {}).items():
            r.conn.execute("""INSERT INTO search_trends(uid,day,value,collected_at) VALUES(?,?,?,?)
                              ON CONFLICT(uid,day) DO UPDATE SET value=excluded.value, collected_at=excluded.collected_at""",
                           (c["uid"], day, v, r.at))
            n += 1
    set_source_status(r.conn, "naver_datalab", True, at=r.at)
    return {"points": n}


# ---------- 5. 점수·가치평가·보고서 ----------

def _company_rows(conn):
    return [dict(x) for x in conn.execute("SELECT * FROM companies")]


def compute(r: Run):
    comps = _company_rows(r.conn)
    groups: dict[tuple, dict] = {}
    stages = {}
    for c in comps:
        facts = registry.get_facts(r.conn, c["uid"])
        st = c["status"] if c["status"] in ("withdrawn", "postponed") else interest.stage_of(facts, r.today)
        stages[c["uid"]] = st
        chs = r.channels_ok.get(c["uid"], set())
        stats = interest.window_stats(r.conn, c["uid"], r.now, chs, name=c["name"])
        bucket = interest.STAGE_BUCKET.get(st, "far")
        groups.setdefault((c["is_sample"], bucket), {})[c["uid"]] = stats
    scored = {}
    for (is_sample, bucket), g in groups.items():
        if len(g) < config.MIN_PEER_GROUP:  # 비교집단이 작으면 같은 데이터 유형 전체와 비교
            pool = {}
            for (s2, _), g2 in groups.items():
                if s2 == is_sample:
                    pool.update(g2)
            res = interest.score_group(pool)
            for uid in g:
                res[uid]["peer_group"] = "전체(단계별 표본 부족)"
                scored[uid] = res[uid]
        else:
            res = interest.score_group(g)
            for uid in g:
                res[uid]["peer_group"] = {"near": "청약·수요예측 임박 단계", "far": "일정 미정·원거리 단계", "done": "상장·철회"}[bucket]
                scored[uid] = res[uid]
    interest.save_scores(r.conn, r.run_id, scored, r.at)

    new_versions = 0
    for c in comps:
        facts = registry.get_facts(r.conn, c["uid"])
        val = valuation.evaluate(facts, c["sector"])
        if val["verdict"] == "판단 유보":
            chk = valuation.underwriter_check(facts)
            if chk:
                chk["peer_engine_reasons"] = val.get("reasons", [])
                val = chk
        sup = valuation.supply_view(facts)
        r.conn.execute("""INSERT INTO valuations(uid,run_id,verdict,detail,computed_at) VALUES(?,?,?,?,?)
                          ON CONFLICT(uid,run_id) DO UPDATE SET verdict=excluded.verdict, detail=excluded.detail""",
                       (c["uid"], r.run_id, val["verdict"], dumps(val), r.at))
        content = report.build(r.conn, c["uid"], stage=stages[c["uid"]], interest=scored.get(c["uid"]),
                               valuation=val, supply=sup, company=c, demand_info=demand.demand_view(facts))
        _, created, _ = report.save(r.conn, c["uid"], content, r.run_id, r.at)
        new_versions += int(created)
    return {"companies": len(comps), "new_report_versions": new_versions}


# ---------- 6. 주간 브리핑 ----------

def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def make_briefing(r: Run):
    from .export import company_rows  # 순환 import 방지
    rows = company_rows(r.conn, r.today)
    since = iso(r.now - timedelta(days=7))
    ch = [dict(x) for x in r.conn.execute(
        """SELECT c.*, co.name, co.is_sample FROM changes c JOIN companies co ON co.uid=c.uid
           WHERE c.detected_at>=? ORDER BY c.detected_at""", (since,))]
    real = [x for x in rows if not x["is_sample"] and x["kind"] == "general"]
    wk_end = (r.today + timedelta(days=7)).isoformat()
    t = r.today.isoformat()
    def in_week(rng):
        return rng and rng.get("start") and rng["start"] <= wk_end and (rng.get("end") or rng["start"]) >= t
    focus = [x for x in real if in_week(x["sched"].get("sched.subscription")) or in_week(x["sched"].get("sched.demand_forecast"))
             or in_week(x["sched"].get("sched.listing"))]
    focus.sort(key=lambda x: (x["sched"].get("sched.subscription") or x["sched"].get("sched.demand_forecast") or {}).get("start") or "9")
    picks = []
    for x in focus:
        picks.append({"uid": x["uid"], "name": x["name"], "stage": x["stage_label"],
                      "dates": {k: v for k, v in x["sched"].items() if v},
                      "why": [p["text"] for p in x["points"][:2]] or ["근거 자료 부족 — 공시 확인 필요"],
                      "watch_out": [p["text"] for p in x["risks"][:2]],
                      "interest": x["interest_score"], "valuation": x["verdict"],
                      "reliability": x["reliability"]})
    by = lambda t: [{"name": c["name"], "uid": c["uid"], "field": c["field"], "before": c["before"], "after": c["after"]}  # noqa: E731
                    for c in ch if c["change_type"] == t and not c["is_sample"]]
    failures = [dict(x) for x in r.conn.execute("SELECT source,status,last_error,last_success FROM source_status WHERE status!='ok'")]
    content = {
        "week_of": week_monday(r.today).isoformat(), "generated_at": r.at, "run_id": r.run_id,
        "headline": f"이번 주(~{wk_end}) 일정 있는 일반기업 {len(picks)}곳",
        "picks": picks,
        "new_companies": by("new"), "schedule_changes": by("schedule_change"), "price_fixed": by("price_fixed"),
        "postponed_withdrawn": by("postponed") + by("withdrawn"), "amendments": by("amendment"),
        "new_risks": by("new_risk"), "source_conflicts": by("source_conflict"),
        "interest_spikes": [{"name": x["name"], "uid": x["uid"], "score": x["interest_score"]} for x in real if x["spike"]],
        "source_failures": failures,
        "caveat": "관심도는 주가 상승 확률이 아니며, 가치평가는 비교기업 기반 상대평가입니다. 투자 판단의 근거로 단독 사용하지 마세요.",
    }
    r.conn.execute("""INSERT INTO briefings(week_of,run_id,content,created_at) VALUES(?,?,?,?)
                      ON CONFLICT(week_of) DO UPDATE SET run_id=excluded.run_id, content=excluded.content,
                        created_at=excluded.created_at""",
                   (content["week_of"], r.run_id, dumps(content), r.at))
    return {"picks": len(picks)}


# ---------- 7. Claude AI 요약 ----------

def run_ai(r: Run):
    from . import ai
    if r.trigger.startswith("catch-up") and not ai.AI_ON_CATCHUP:
        return {"skipped_reason": "서버 재시작 직후 수집에서는 AI 요약을 건너뜀(IPO_RADAR_AI_ON_CATCHUP=1로 변경 가능)"}
    try:
        client_ok = ai.make_client()
    except ai.NotConfigured as e:
        set_source_status(r.conn, "claude_api", None, str(e), status="not_configured", at=r.at)
        return {"degraded": True, "error": str(e)}
    del client_ok
    reports = {}
    for row in r.conn.execute("""SELECT r.uid, r.content FROM reports r JOIN companies c USING(uid)
                                 WHERE c.is_sample=0 AND c.kind='general' AND c.status!='withdrawn'
                                   AND r.version=(SELECT MAX(version) FROM reports WHERE uid=r.uid)"""):
        reports[row["uid"]] = loads(row["content"])
    b = r.conn.execute("SELECT content FROM briefings ORDER BY week_of DESC LIMIT 1").fetchone()
    res = ai.refresh(r.conn, r, reports, loads(b["content"]) if b else None)
    if res["errors"] and not res["generated"]:
        set_source_status(r.conn, "claude_api", False, res["errors"][0][:300], at=r.at)
        return {"degraded": True, "error": res["errors"][0][:200], **res}
    set_source_status(r.conn, "claude_api", True, at=r.at, note=f"생성 {res['generated']} · 재사용 {res['skipped']}")
    return {**res, "degraded": bool(res["errors"])}


# ---------- 실행 ----------

def run(*, now: datetime | None = None, trigger: str = "manual", db_path=None, with_sample: bool = True,
        skip: set[str] | None = None, export_dir=None) -> dict:
    from . import export, sample
    skip = skip or set()
    now = now or datetime.now(config.KST)
    conn = connect(db_path)
    reset_run_state()
    r = Run(conn, now, trigger)
    steps = [
        ("seed", load_seed_files), ("ipostock", collect_ipostock), ("dart", verify_dart),
        ("financials", refresh_financials), ("buzz", refresh_buzz), ("search", refresh_search),
    ]
    for name, fn in steps:
        if name not in skip:
            r.stage(name, fn, r)
    if with_sample:
        r.stage("sample", sample.load, r)
    r.stage("compute", compute, r)
    r.stage("briefing", make_briefing, r)
    if "ai" not in skip:
        r.stage("ai", run_ai, r)
    status = r.finish()
    if "export" not in skip:
        export.write(conn, now=now, out_dir=export_dir)
    conn.close()
    return {"run_id": r.run_id, "status": status, "stages": r.stages}
