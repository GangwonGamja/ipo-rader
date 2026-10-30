"""IPO Radar 웹 서버 — 로그인 보호 + 주간 자동 갱신 + 관리자 수동 갱신.

    python -m ipo_radar serve-prod        # 운영 서버 (기본 0.0.0.0:8080)

- 인증: HTTP Basic(반드시 HTTPS 뒤에서 사용). 사용자·비밀번호 해시는 환경변수 IPO_RADAR_USERS.
  로그인 10회 실패한 IP는 15분간 차단.
- 제공 경로(허용 목록만): /  /data.json  /ipo_calendar.ics  /ipo_list.csv  /api/status  /healthz
  POST /admin/refresh (admin 역할만)
- 스케줄러: 매주 월 08:00 KST 실행, 전체 실패 시 30분 간격 최대 3회 재시도.
  서버가 꺼져 있어 지난 실행을 놓쳤으면(마지막 성공이 7일 넘게 지남) 시작 직후 실행.
- 같은 수집이 동시에 두 번 돌지 않도록 프로세스 내 잠금 + 파일 잠금.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config
from .db import connect, iso

PBKDF2_ITER = 260_000


# ---------- 비밀번호 ----------

def hash_password(pw: str, salt: str | None = None, iterations: int = PBKDF2_ITER) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, it, salt, h = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False


def parse_users(spec: str) -> dict[str, dict]:
    users = {}
    for part in filter(None, (x.strip() for x in spec.split(";"))):
        fields = part.split(":")
        if len(fields) < 2:
            continue
        users[fields[0]] = {"hash": fields[1], "role": fields[2] if len(fields) > 2 else "viewer"}
    return users


class LoginGuard:
    """IP별 로그인 실패 횟수 제한."""

    def __init__(self, limit=10, window=900):
        self.limit, self.window, self.fails, self.lock = limit, window, {}, threading.Lock()

    def blocked(self, ip):
        with self.lock:
            ts = [t for t in self.fails.get(ip, []) if time.time() - t < self.window]
            self.fails[ip] = ts
            return len(ts) >= self.limit

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    def ok(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


# ---------- 수집 실행기 ----------

class Runner:
    def __init__(self, out_dir: Path, with_sample: bool):
        self.out_dir, self.with_sample = out_dir, with_sample
        self.lock = threading.Lock()
        self.state = {"running": False, "last": None, "last_error": None, "next": None, "started_at": None}

    def _file_lock(self):
        p = Path(config.DB_PATH).with_suffix(".lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        f = open(p, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 다른 프로세스가 실행 중이면 BlockingIOError
        return f

    def run(self, trigger: str) -> dict | None:
        if not self.lock.acquire(blocking=False):
            return None
        try:
            fl = self._file_lock()
        except BlockingIOError:
            self.lock.release()
            return None
        self.state.update(running=True, started_at=iso())
        try:
            from .__main__ import run_with_recovery
            res = run_with_recovery(trigger=trigger, with_sample=self.with_sample, export_dir=self.out_dir,
                                    retries=3 if trigger == "schedule" else 1)
            self.state.update(last={"run_id": res["run_id"], "status": res["status"], "finished_at": iso()}, last_error=None)
            log(f"수집 완료 {res['run_id']} {res['status']}")
            return res
        except Exception as e:
            self.state["last_error"] = f"{type(e).__name__}: {e}"
            log("수집 오류\n" + traceback.format_exc())
            return None
        finally:
            self.state["running"] = False
            fcntl.flock(fl, fcntl.LOCK_UN)
            fl.close()
            self.lock.release()

    def run_async(self, trigger: str) -> bool:
        if self.state["running"]:
            return False
        threading.Thread(target=self.run, args=(trigger,), daemon=True).start()
        return True

    def needs_catch_up(self) -> bool:
        try:
            conn = connect()
            row = conn.execute("SELECT started_at FROM runs WHERE status IN ('success','partial') ORDER BY started_at DESC LIMIT 1").fetchone()
            conn.close()
        except Exception:
            return True
        if not row:
            return True
        last = datetime.fromisoformat(row["started_at"])
        return datetime.now(config.KST) - last > timedelta(days=7) or not (self.out_dir / "index.html").exists()

    def scheduler(self):
        from .__main__ import next_run
        if self.needs_catch_up():
            log("마지막 성공 실행이 7일을 넘었거나 대시보드가 없어 즉시 수집합니다")
            self.run("catch-up")
        while True:
            nxt = next_run(datetime.now(config.KST))
            self.state["next"] = iso(nxt)
            log(f"다음 자동 수집 {iso(nxt)}")
            while datetime.now(config.KST) < nxt:
                time.sleep(min(300, max(1, (nxt - datetime.now(config.KST)).total_seconds())))
            self.run("schedule")


def log(msg: str):
    print(f"[{iso()}] {msg}", flush=True)


# ---------- HTTP ----------

STATIC = {
    "/data.json": ("data.json", "application/json; charset=utf-8"),
    "/ipo_calendar.ics": ("ipo_calendar.ics", "text/calendar; charset=utf-8"),
    "/ipo_list.csv": ("ipo_list.csv", "text/csv; charset=utf-8"),
}
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "IPORadar"
        sys_version = ""

        def log_message(self, fmt, *args):
            log(f"{self.client_address[0]} {fmt % args}")

        def _ip(self):
            return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip() \
                if app["trust_proxy"] else self.client_address[0]

        def _send(self, code, body: bytes, ctype="text/plain; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _auth(self):
            if app["anon"]:
                return {"name": "anonymous", "role": "admin"}
            ip = self._ip()
            if app["guard"].blocked(ip):
                self._send(429, "로그인 실패가 많아 잠시 차단되었습니다. 15분 후 다시 시도하세요.".encode())
                return None
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    name, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                except Exception:
                    name, pw = "", ""
                u = app["users"].get(name)
                if u and verify_password(pw, u["hash"]):
                    app["guard"].ok(ip)
                    return {"name": name, "role": u["role"]}
                app["guard"].fail(ip)
            self._send(401, "로그인이 필요합니다.".encode(), extra={"WWW-Authenticate": 'Basic realm="IPO Radar", charset="UTF-8"'})
            return None

        def _file(self, name, ctype):
            p = app["out"] / name
            if not p.is_file():
                return self._send(503, preparing_page(app).encode("utf-8"), "text/html; charset=utf-8")
            self._send(200, p.read_bytes(), ctype)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/healthz":
                return self._send(200, b"ok")
            user = self._auth()
            if not user:
                return
            if path in ("/", "/index.html"):
                return self._file(app["index"], "text/html; charset=utf-8")
            if path in STATIC:
                return self._file(*STATIC[path])
            if path == "/api/status":
                body = {"user": user["name"], "role": user["role"], **app["runner"].state}
                dj = app["out"] / "data.json"
                if dj.is_file():
                    try:
                        body["generated_at"] = json.loads(dj.read_text(encoding="utf-8"))["meta"]["generated_at"]
                    except Exception:
                        pass
                return self._send(200, json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            self._send(404, b"not found")

        def do_POST(self):
            path = self.path.split("?")[0]
            user = self._auth()
            if not user:
                return
            if path == "/admin/refresh":
                if user["role"] != "admin":
                    return self._send(403, "관리자만 수집을 실행할 수 있습니다.".encode())
                if self.headers.get("X-Requested-With") != "ipo-radar":  # 간단한 CSRF 방지(같은 출처 스크립트만 헤더 지정 가능)
                    return self._send(400, b"bad request")
                started = app["runner"].run_async(f"manual-{user['name']}")
                return self._send(202 if started else 409, json.dumps(
                    {"started": started, "message": "수집을 시작했습니다" if started else "이미 수집 중입니다"},
                    ensure_ascii=False).encode(), "application/json; charset=utf-8")
            self._send(404, b"not found")
    return H


def preparing_page(app) -> str:
    """대시보드 파일이 아직 없을 때: 진행 단계·경과 시간·실패 사유를 보여 주고 15초마다 새로고침."""
    import html
    from . import pipeline
    st, runner = pipeline.PROGRESS, app["runner"]
    if runner.state["running"] and st.get("stage"):
        since = datetime.fromisoformat(st["since"]) if st.get("since") else None
        mins = int((datetime.now(config.KST) - since).total_seconds() // 60) if since else 0
        msg = f"수집 중입니다 — 현재 단계: {pipeline.STAGE_LABEL.get(st['stage'], st['stage'])} ({mins}분째)"
    elif runner.state["running"]:
        msg = "수집을 시작하는 중입니다."
    elif runner.state.get("last_error"):
        msg = f"수집이 실패해 대시보드를 만들지 못했습니다: {runner.state['last_error'][:300]} — 잠시 후 자동으로 다시 시도합니다."
    else:
        msg = "수집을 시작합니다."
    now = time.time()
    if not runner.state["running"] and now - app.get("last_kick", 0) > 300:  # 5분마다 한 번 자동 재시도
        app["last_kick"] = now
        runner.run_async("catch-up")
    return ("<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='15'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'><title>IPO Radar 준비 중</title>"
            "<body style='font-family:system-ui,sans-serif;padding:32px;color:#231815;background:#f6f4f1'>"
            "<h2 style='margin:0 0 12px'>대시보드를 준비하고 있습니다</h2>"
            f"<p>{html.escape(msg)}</p><p style='color:#8a7c72'>이 화면은 15초마다 자동으로 새로고침됩니다. "
            "서버가 잠들어 있다가 깨어난 경우 첫 수집에 1~5분 걸릴 수 있습니다.</p></body>")


def build_app(out_dir: Path | None = None, users_spec: str | None = None, anon: bool | None = None,
              with_sample: bool | None = None, trust_proxy: bool | None = None) -> dict:
    out = Path(out_dir or config.OUTPUT_DIR)
    users = parse_users(config.SERVER_USERS if users_spec is None else users_spec)
    if users_spec is None and config.SERVER_ADMIN_USER and config.SERVER_ADMIN_PASSWORD:
        if len(config.SERVER_ADMIN_PASSWORD) < 10:
            raise SystemExit("IPO_RADAR_ADMIN_PASSWORD는 10자 이상이어야 합니다.")
        users[config.SERVER_ADMIN_USER] = {"hash": hash_password(config.SERVER_ADMIN_PASSWORD), "role": "admin"}
    anon = config.SERVER_ALLOW_ANON if anon is None else anon
    if not users and not anon:
        raise SystemExit("로그인 계정이 없습니다. IPO_RADAR_ADMIN_USER/IPO_RADAR_ADMIN_PASSWORD 또는 IPO_RADAR_USERS를 설정하세요.")
    brand = config.DATA_DIR / "brand.json"
    index = "index.html"
    if brand.exists() and config.SERVER_BRAND:
        index = Path(json.loads(brand.read_text(encoding="utf-8")).get("output", "branch.html")).name
    return {"out": out, "users": users, "anon": anon, "guard": LoginGuard(), "index": index,
            "runner": Runner(out, config.SERVER_WITH_SAMPLE if with_sample is None else with_sample),
            "trust_proxy": os.environ.get("IPO_RADAR_TRUST_PROXY", "1") == "1" if trust_proxy is None else trust_proxy}


def serve(host: str | None = None, port: int | None = None):
    app = build_app()
    if config.SERVER_SCHEDULER:
        threading.Thread(target=app["runner"].scheduler, daemon=True).start()
    srv = ThreadingHTTPServer((host or config.SERVER_HOST, port or config.SERVER_PORT), make_handler(app))
    log(f"IPO Radar 서버 시작 http://{srv.server_address[0]}:{srv.server_address[1]} · 사용자 {len(app['users'])}명 · 첫 화면 {app['index']}")
    srv.serve_forever()
