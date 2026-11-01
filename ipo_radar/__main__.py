"""IPO Radar CLI.

  python -m ipo_radar run [--no-sample] [--now 2026-11-02T08:00:00+09:00] [--trigger manual|schedule]
  python -m ipo_radar status            # 소스별 상태·최근 실행
  python -m ipo_radar export            # DB → dashboard/index.html, data.json
  python -m ipo_radar serve --port 8000 # 대시보드 로컬 확인
  python -m ipo_radar next-run          # 다음 예약 실행 시각(KST)
  python -m ipo_radar daemon            # 내장 스케줄러(매주 월 08:00 KST, 실패 시 30분 간격 최대 3회 재실행)
  python -m ipo_radar probe ipostock    # 실제 페이지 구조로 파서 점검
  python -m ipo_radar watch add 바로팜 | watch rm 바로팜 | watch ls
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import sys
import time
from datetime import datetime, timedelta

from . import config
from .db import connect, iso


def next_run(now: datetime) -> datetime:
    now = now.astimezone(config.KST)
    cand = now.replace(hour=config.SCHEDULE_HOUR, minute=0, second=0, microsecond=0)
    cand += timedelta(days=(config.SCHEDULE_WEEKDAY - now.weekday()) % 7)
    if cand <= now:
        cand += timedelta(days=7)
    return cand


def next_daily(now: datetime) -> datetime:
    now = now.astimezone(config.KST)
    cand = now.replace(hour=config.DAILY_HOUR, minute=config.DAILY_MINUTE, second=0, microsecond=0)
    return cand if cand > now else cand + timedelta(days=1)


def prev_daily(now: datetime) -> datetime:
    return next_daily(now) - timedelta(days=1)


def run_with_recovery(trigger="schedule", retries=3, wait_sec=1800, run_fn=None, sleep=time.sleep, **kw):
    """실행 결과가 failed이면 wait_sec 후 재실행. partial은 다음 주기에 소스별로 다시 시도한다."""
    from . import pipeline
    run_fn = run_fn or pipeline.run
    res = None
    for i in range(retries):
        res = run_fn(trigger=trigger if i == 0 else f"{trigger}-retry{i}", **kw)
        if res["status"] != "failed":
            return res
        if i < retries - 1:
            sleep(wait_sec)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ipo_radar")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--no-sample", action="store_true")
    p.add_argument("--now")
    p.add_argument("--trigger", default="manual")
    p.add_argument("--skip", default="")
    p.add_argument("--recover", action="store_true", help="전체 실패 시 30분 간격으로 최대 3회 재실행")
    sub.add_parser("status")
    sub.add_parser("export")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("next-run")
    sub.add_parser("serve-prod", help="운영 웹 서버(로그인·자동 갱신)")
    hp = sub.add_parser("hash-password", help="사용자 비밀번호 해시 생성")
    hp.add_argument("name")
    sub.add_parser("daemon")
    pr = sub.add_parser("probe")
    pr.add_argument("source", choices=["ipostock", "dart", "naver", "krx"])
    w = sub.add_parser("watch")
    w.add_argument("action", choices=["add", "rm", "ls"])
    w.add_argument("name", nargs="?")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        from . import pipeline
        now = datetime.fromisoformat(a.now) if a.now else None
        kw = dict(now=now, with_sample=not a.no_sample, skip={x for x in a.skip.split(",") if x})
        res = run_with_recovery(trigger=a.trigger, **kw) if a.recover else pipeline.run(trigger=a.trigger, **kw)
        print(json.dumps({k: v for k, v in res.items()}, ensure_ascii=False, indent=1, default=str)[:4000])
        return 0 if res["status"] != "failed" else 1
    if a.cmd == "status":
        conn = connect()
        print("[소스 상태]")
        for r in conn.execute("SELECT * FROM source_status ORDER BY source"):
            print(f"  {r['source']:<18} {r['status']:<15} 마지막 성공: {r['last_success'] or '-'}  오류: {(r['last_error'] or '')[:90]}")
        print("[최근 실행]")
        for r in conn.execute("SELECT run_id,status,started_at,finished_at FROM runs ORDER BY started_at DESC LIMIT 5"):
            print(f"  {r['run_id']:<32} {r['status']:<8} {r['started_at']} → {r['finished_at']}")
        print("다음 예약 실행:", iso(next_run(datetime.now(config.KST))))
        return 0
    if a.cmd == "export":
        from . import export
        print(export.write(connect()))
        return 0
    if a.cmd == "serve":
        h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(config.OUTPUT_DIR))
        print(f"http://localhost:{a.port}/index.html")
        http.server.ThreadingHTTPServer(("0.0.0.0", a.port), h).serve_forever()
    if a.cmd == "serve-prod":
        from . import server
        server.serve()
        return 0
    if a.cmd == "hash-password":
        import getpass
        from . import server
        pw = getpass.getpass("비밀번호: ")
        if len(pw) < 10:
            print("비밀번호는 10자 이상으로 정하세요.")
            return 1
        if pw != getpass.getpass("다시 입력: "):
            print("두 비밀번호가 다릅니다.")
            return 1
        print(f"{a.name}:{server.hash_password(pw)}:viewer   (관리자는 끝을 :admin 으로)")
        return 0
    if a.cmd == "next-run":
        print(iso(next_run(datetime.now(config.KST))))
        return 0
    if a.cmd == "daemon":
        while True:
            nxt = next_run(datetime.now(config.KST))
            print(f"[{iso()}] 다음 실행 {iso(nxt)}", flush=True)
            while datetime.now(config.KST) < nxt:
                time.sleep(min(600, max(1, (nxt - datetime.now(config.KST)).total_seconds())))
            res = run_with_recovery()
            print(f"[{iso()}] 실행 완료: {res['run_id']} {res['status']}", flush=True)
    if a.cmd == "probe":
        from datetime import date
        if a.source == "ipostock":
            from .collectors import ipostock
            from .http import fetch
            for y, m in ipostock.months_to_scan(date.today()):
                url = ipostock.month_url(y, m)
                try:
                    html = fetch(url)
                    rows = ipostock.parse_calendar(html, y, m)
                    print(url, f"→ {len(rows)}건 인식, HTML {len(html)}자")
                    for r in rows[:15]:
                        print("   ", r)
                    if not rows:
                        print("   ⚠ 인식된 일정 없음: HTML 구조 확인 필요 (앞 1500자)\n", html[:1500])
                except Exception as e:
                    print(url, "실패:", e)
        elif a.source == "dart":
            from .collectors import dart
            print(dart.list_equity_filings(date.today())[:5])
        elif a.source == "naver":
            from .collectors import buzz
            print(buzz.collect_for("공모주")[1])
        elif a.source == "krx":
            from .collectors import krx
            snap = krx.market_snapshot(date.today())
            print(len(snap), list(snap.items())[:3])
        return 0
    if a.cmd == "watch":
        conn = connect()
        if a.action == "ls":
            for r in conn.execute("SELECT w.*, c.name FROM watchlist w JOIN companies c USING(uid)"):
                print(r["name"], r["added_at"])
            return 0
        from .registry import normalize_name
        row = conn.execute("SELECT uid FROM company_aliases WHERE alias=?", (normalize_name(a.name or ""),)).fetchone()
        if not row:
            print("기업을 찾을 수 없음:", a.name)
            return 1
        if a.action == "add":
            conn.execute("INSERT OR IGNORE INTO watchlist(uid,added_at) VALUES(?,?)", (row["uid"], iso()))
        else:
            conn.execute("DELETE FROM watchlist WHERE uid=?", (row["uid"],))
        conn.commit()
        return 0


if __name__ == "__main__":
    sys.exit(main())
