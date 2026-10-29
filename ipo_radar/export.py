"""DB → 대시보드 데이터(data.json) + 단일 파일 대시보드(index.html) 생성."""
from __future__ import annotations

import base64
import csv
import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config
from .db import iso, loads
from .interest import STAGE_LABEL
from .registry import SCHEDULE_KEYS, get_facts

KIND_LABEL = {"general": "일반", "spac": "스팩", "reits": "리츠"}
VER_LABEL = {"official": "공식 공시 확인", "aggregator": "집계 사이트", "websearch": "웹검색 요약(원문 미확인)",
             "unverified": "미확인"}


def _latest_report(conn, uid):
    r = conn.execute("SELECT content, diff, version, created_at FROM reports WHERE uid=? ORDER BY version DESC LIMIT 1",
                     (uid,)).fetchone()
    if not r:
        return None
    c = loads(r["content"])
    c["diff_from_previous"] = loads(r["diff"]) or []
    c["versions"] = [dict(v) for v in conn.execute(
        "SELECT version, created_at, run_id FROM reports WHERE uid=? ORDER BY version", (uid,))]
    return c


def _scores(conn, uid):
    return [dict(x) for x in conn.execute(
        """SELECT i.run_id, i.score, i.computed_at FROM interest_scores i WHERE uid=? ORDER BY computed_at""", (uid,))]


def company_rows(conn, today: date) -> list[dict]:
    out = []
    for c in conn.execute("SELECT * FROM companies ORDER BY name"):
        c = dict(c)
        facts = get_facts(conn, c["uid"])
        rep = _latest_report(conn, c["uid"]) or {}
        hist = _scores(conn, c["uid"])
        valid = [h for h in hist if h["score"] is not None]
        cur = hist[-1] if hist else None
        prev_valid = valid[-2]["score"] if len(valid) >= 2 else None
        interest = rep.get("interest") or {}
        last_upd = max([f["collected_at"] for f in facts.values()] + [c["last_seen"]])
        stale = (datetime.fromisoformat(last_upd).date() < today - timedelta(days=config.STALE_DAYS))
        missing = len(rep.get("missing", []))
        if c["is_sample"]:
            rel = "예시"
        elif c["verification"] == "official":
            rel = "높음" if missing <= 4 else "보통"
        elif c["verification"] == "aggregator":
            rel = "보통" if missing <= 6 else "낮음"
        else:
            rel = "낮음"
        band = (facts.get("offer.price_band") or {}).get("value")
        final = (facts.get("offer.price_final") or {}).get("value")
        stage = rep.get("stage") or "watch"
        val = rep.get("valuation") or {}
        out.append({
            "uid": c["uid"], "name": c["name"], "kind": c["kind"], "kind_label": KIND_LABEL[c["kind"]],
            "market": c["market"], "sector": c["sector"], "underwriters": c["underwriters"],
            "status": c["status"], "is_sample": bool(c["is_sample"]),
            "stage": stage, "stage_label": STAGE_LABEL.get(stage, stage),
            "sched": {k: (facts.get(k) or {}).get("value") for k in SCHEDULE_KEYS},
            "price_band": band, "price_final": final,
            "market_cap": rep.get("market_cap_estimate"),
            "interest_score": cur["score"] if cur else None,
            "interest_prev": prev_valid,
            "interest_last_valid": valid[-1] if valid else None,
            "interest_reliability": interest.get("reliability"),
            "spike": bool(interest.get("spike")),
            "verdict": val.get("verdict", "판단 유보"),
            "verdict_premium": val.get("premium") if val.get("engine") == "underwriter_check" else ((val.get("scenarios") or {}).get("기준") or {}).get("premium_vs_offer"),
            "fair_range": val.get("fair_range"), "eff_discount": val.get("eff_discount"),
            "verdict_provisional": bool(val.get("provisional")), "forecast_dependency": val.get("forecast_dependency"),
            "ref_verdict": (val.get("reference") or {}).get("verdict"),
            "demand_score": (rep.get("demand") or {}).get("score"), "demand_grade": (rep.get("demand") or {}).get("grade"),
            "verification": c["verification"], "verification_label": VER_LABEL[c["verification"]],
            "reliability": rel, "missing_count": missing,
            "last_updated": last_upd, "stale": stale,
            "points": (rep.get("summary") or {}).get("points", []),
            "risks": (rep.get("summary") or {}).get("risks", []),
            "first_seen": c["first_seen"],
            "summary": (facts.get("profile.summary") or facts.get("profile.business") or {}).get("value"),
            "listing_type": (facts.get("profile.listing_type") or {}).get("value"),
            "min_subscription": (facts.get("offer.min_subscription") or {}).get("value"),
            "deposit_rate": (facts.get("offer.deposit_rate") or {}).get("value"),
            "syndicate": (facts.get("offer.syndicate") or {}).get("value"),
        })
    return out


def _mention_daily(conn, uid, today):
    start = today - timedelta(days=29)
    days = {(start + timedelta(days=i)).isoformat(): {"news": 0, "blog": 0, "cafe": 0, "video": 0, "community": 0} for i in range(30)}
    for r in conn.execute("""SELECT channel, substr(published_at,1,10) d FROM mentions
                             WHERE uid=? AND relevant=1 AND is_duplicate=0""", (uid,)):
        if r["d"] in days:
            days[r["d"]][r["channel"]] = days[r["d"]].get(r["channel"], 0) + 1
    return [{"date": d, **v} for d, v in days.items()]


def _ai_notes(conn, real_uids: set) -> dict:
    from .ai import load_notes
    return {k: v for k, v in load_notes(conn).items() if k in real_uids or k == "__weekly__"}


def build_data(conn, now: datetime | None = None) -> dict:
    now = now or datetime.now(config.KST)
    today = now.astimezone(config.KST).date()
    rows = company_rows(conn, today)
    end90 = (today + timedelta(days=config.LOOKAHEAD_DAYS)).isoformat()
    t = today.isoformat()
    wk_end = (today + timedelta(days=7)).isoformat()
    run = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    last_ok = conn.execute("SELECT * FROM runs WHERE status IN ('success','partial') ORDER BY started_at DESC LIMIT 1").fetchone()
    since7 = iso(now - timedelta(days=7))
    changes = [dict(x) for x in conn.execute(
        """SELECT c.id, c.run_id, c.uid, co.name, co.is_sample, c.change_type, c.field, c.before, c.after,
                  c.detected_at, s.url AS source_url, s.publisher AS source_publisher
           FROM changes c JOIN companies co ON co.uid=c.uid LEFT JOIN sources s ON s.source_id=c.source_id
           WHERE c.detected_at>=? ORDER BY c.detected_at DESC, c.id DESC""", (iso(now - timedelta(days=120)),))]

    def upcoming(x):
        for k in ("sched.demand_forecast", "sched.subscription", "sched.listing"):
            v = x["sched"].get(k)
            if v and (v.get("end") or v.get("start")) and (v.get("end") or v.get("start")) >= t and (v.get("start") or v.get("end")) <= end90:
                return True
        return False

    def this_week(x, k):
        v = x["sched"].get(k)
        return bool(v and v.get("start") and v["start"] <= wk_end and (v.get("end") or v["start"]) >= t)

    def kpis(sample: bool):
        rs = [x for x in rows if x["is_sample"] == sample and x["kind"] == "general"]
        ch = [c for c in changes if bool(c["is_sample"]) == sample and c["detected_at"] >= since7]
        return {
            "upcoming_90d": sum(1 for x in rs if upcoming(x) and x["status"] not in ("withdrawn",)),
            "watch_undated": sum(1 for x in rs if not any(x["sched"].values()) or x["status"] == "postponed"),
            "this_week_subscription": [x["name"] for x in rs if this_week(x, "sched.subscription")],
            "this_week_demand": [x["name"] for x in rs if this_week(x, "sched.demand_forecast")],
            "this_week_listing": [x["name"] for x in rs if this_week(x, "sched.listing")],
            "new_7d": sum(1 for c in ch if c["change_type"] == "new"),
            "changed_7d": sum(1 for c in ch if c["change_type"] in ("schedule_change", "price_fixed", "postponed", "withdrawn", "amendment")),
            "spikes": [x["name"] for x in rs if x["spike"]],
            "spac_reits_hidden": sum(1 for x in rows if x["is_sample"] == sample and x["kind"] != "general"),
        }

    cal = []
    for x in rows:
        for k, lab in SCHEDULE_KEYS.items():
            v = x["sched"].get(k)
            if v and (v.get("start") or v.get("end")):
                cal.append({"uid": x["uid"], "name": x["name"], "event": k, "label": lab,
                            "start": v.get("start") or v.get("end"), "end": v.get("end") or v.get("start"),
                            "is_sample": x["is_sample"], "kind": x["kind"]})
    briefs = [loads(b["content"]) for b in conn.execute("SELECT content FROM briefings ORDER BY week_of DESC LIMIT 8")]
    return {
        "meta": {
            "generated_at": iso(now), "today": t, "lookahead_end": end90, "week_end": wk_end,
            "weights": config.INTEREST_WEIGHTS, "growth_prior": config.GROWTH_PRIOR,
            "stale_days": config.STALE_DAYS,
            "schedule": "매주 월요일 08:00 (Asia/Seoul)",
            "last_run": dict(run) if run else None, "last_ok_run": dict(last_ok) if last_ok else None,
        },
        "sources": [dict(x) for x in conn.execute("SELECT * FROM source_status ORDER BY source")],
        "kpis": {"real": kpis(False), "sample": kpis(True)},
        "companies": rows,
        "reports": {x["uid"]: _latest_report(conn, x["uid"]) for x in rows},
        "interest_history": {x["uid"]: _scores(conn, x["uid"]) for x in rows},
        "mention_daily": {x["uid"]: _mention_daily(conn, x["uid"], today) for x in rows},
        "search_trend": {x["uid"]: [{"day": r["day"], "value": r["value"]} for r in conn.execute(
            "SELECT day, value FROM search_trends WHERE uid=? AND day>=? ORDER BY day", (x["uid"], (today - timedelta(days=60)).isoformat()))]
            for x in rows},
        "changes": changes,
        "calendar": cal,
        "briefings": briefs,
        "watchlist": [dict(x) for x in conn.execute("SELECT * FROM watchlist")],
        "ai": _ai_notes(conn, {x["uid"] for x in rows if not x["is_sample"]}),
    }


def load_brand() -> dict | None:
    """data/brand.json이 있으면 지점용 브랜드 대시보드도 만든다."""
    p = config.DATA_DIR / "brand.json"
    if not p.exists():
        return None
    b = json.loads(p.read_text(encoding="utf-8"))
    logo = config.ROOT / b.get("logo", "")
    if b.get("logo") and logo.is_file():
        b["logo_data"] = "data:image/png;base64," + base64.b64encode(logo.read_bytes()).decode()
    return b


def ics(data: dict, only_real: bool = True) -> str:
    """청약·환불·상장 등 일정을 캘린더(ICS) 파일로. 종일 일정."""
    stamp = data["meta"]["generated_at"].replace("-", "").replace(":", "")[:15]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//IPO Radar//KO", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:IPO Radar 공모주 일정", "X-WR-TIMEZONE:Asia/Seoul"]
    for e in data["calendar"]:
        if only_real and e["is_sample"]:
            continue
        end = (date.fromisoformat(e["end"]) + timedelta(days=1)).strftime("%Y%m%d")
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}-{e['event']}@ipo-radar", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{e['start'].replace('-', '')}", f"DTEND;VALUE=DATE:{end}",
                  f"SUMMARY:[{e['label']}] {e['name']}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def csv_text(data: dict, house: str | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["기업명", "구분", "업종", "단계", "수요예측", "공모가 확정", "청약", "환불", "상장예정", "희망공모가 하단",
                "희망공모가 상단", "확정공모가", "주관사", "당사 주관", "관심도", "가치평가", "데이터 신뢰도", "마지막 갱신", "예시 여부"])
    for c in data["companies"]:
        sc = c["sched"]
        r = lambda k: "" if not sc.get(k) else f"{sc[k].get('start') or ''}~{sc[k].get('end') or ''}"  # noqa: E731
        w.writerow([c["name"], c["kind_label"], c["sector"] or "", c["stage_label"], r("sched.demand_forecast"),
                    r("sched.price_fix"), r("sched.subscription"), r("sched.refund"), r("sched.listing"),
                    (c["price_band"] or {}).get("low", ""), (c["price_band"] or {}).get("high", ""), c["price_final"] or "",
                    c["underwriters"] or "", "Y" if house and house in (c["underwriters"] or "") else "",
                    "" if c["interest_score"] is None else c["interest_score"], c["verdict"], c["reliability"],
                    c["last_updated"], "Y" if c["is_sample"] else ""])
    return "\ufeff" + buf.getvalue()  # 엑셀 한글 깨짐 방지(BOM)


def _render(tpl: str, data: dict) -> str:
    js = json.dumps(data, ensure_ascii=False, default=str)
    return tpl.replace("/*__IPO_RADAR_DATA__*/null", js.replace("</", "<\\/"))


def write(conn, now: datetime | None = None, out_dir: Path | str | None = None) -> Path:
    out = Path(out_dir or config.OUTPUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    data = build_data(conn, now)
    data["meta"]["brand"] = None
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    (out / "ipo_calendar.ics").write_text(ics(data), encoding="utf-8")
    tpl = (config.DASHBOARD_DIR / "template.html").read_text(encoding="utf-8")
    (out / "index.html").write_text(_render(tpl, data), encoding="utf-8")
    brand = load_brand()
    (out / "ipo_list.csv").write_text(csv_text(data, brand and brand.get("house")), encoding="utf-8")
    if brand:
        data["meta"]["brand"] = {k: brand.get(k) for k in ("house", "branch", "logo_data")}
        target = Path(brand.get("output", "dashboard/branch.html"))
        target = target if target.is_absolute() else (out / target.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render(tpl, data), encoding="utf-8")  # 공유 폴더 절대경로 지정 가능
    return out / "index.html"
