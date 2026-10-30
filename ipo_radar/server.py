"""IPO Radar 웹 서버 — 로그인 보호 + 주간 자동 갱신 + 관리자 수동 갱신.

    python -m ipo_radar serve-prod        # 운영 서버 (기본 0.0.0.0:8080)

- 접속: /login 화면에서 '접속 코드' 입력. IPO_RADAR_ADMIN_CODE → 관리자 모드, IPO_RADAR_USER_CODE → 이용자 모드.
  서명된 세션 쿠키(HttpOnly·SameSite=Strict·HTTPS면 Secure, 기본 12시간). 코드 10회 실패한 IP는 15분간 차단.
- 이용자 모드에는 자료원 상태·오류·실행 기록을 뺀 별도 화면(*_view.html, data_view.json)을 보낸다.
- 제공 경로(허용 목록만): /  /login  /logout  /data.json  /ipo_calendar.ics  /ipo_list.csv  /api/status  /healthz
  POST /login, POST /admin/refresh (관리자만)
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


# ---------- 접속 코드·세션 ----------

SESSION_COOKIE = "ipo_session"
SESSION_HOURS = int(os.environ.get("IPO_RADAR_SESSION_HOURS", "12"))


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(app, role: str, now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_HOURS * 3600)
    payload = f"{role}|{exp}".encode()
    sig = hmac.new(app["keys"][role], payload, hashlib.sha256).hexdigest()
    return f"{_b64(payload)}.{sig}"


def read_token(app, token: str | None, now: float | None = None) -> str | None:
    """서명이 맞고 만료되지 않았으면 역할(admin/viewer) 반환."""
    try:
        p, sig = (token or "").split(".", 1)
        payload = _unb64(p)
        role, exp = payload.decode().split("|")
        if role not in ("admin", "viewer"):
            return None
        if not hmac.compare_digest(hmac.new(app["keys"][role], payload, hashlib.sha256).hexdigest(), sig):
            return None
        if int(exp) < (now or time.time()):
            return None
        return role
    except Exception:
        return None


def check_code(app, code: str, mode: str | None = None) -> str | None:
    """접속 코드 → 역할. mode(admin/viewer)를 주면 그 모드의 코드만 받는다."""
    code = (code or "").strip()
    if not code:
        return None
    for c in app["codes"]:
        if mode and c["role"] != mode:
            continue
        secret = c["secret"]
        ok = verify_password(code, secret) if secret.startswith("pbkdf2_sha256$") else \
            hmac.compare_digest(code.encode("utf-8"), secret.encode("utf-8"))
        if ok:
            return c["role"]
    return None


def role_keys(codes: list[dict]) -> dict:
    """역할마다 다른 서명 키. 이용자 코드를 바꾸면 이용자 세션만 끊기고 관리자 세션은 유지된다."""
    extra = os.environ.get("IPO_RADAR_SECRET", "")
    keys = {}
    for role in ("admin", "viewer"):
        src = "|".join(sorted(c["secret"] for c in codes if c["role"] == role))
        keys[role] = hashlib.sha256(f"ipo-radar-session|{role}|{src}|{extra}".encode("utf-8")).digest()
    return keys


# ---------- 이용자 코드: 관리자 화면에서 변경 ----------

def access_file() -> Path:
    return Path(config.DB_PATH).parent / "access.json"


def load_access() -> dict:
    try:
        return json.loads(access_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def render_sync(value: str | None, name: str = "IPO_RADAR_USER_CODE") -> tuple[bool | None, str]:
    """Render 환경변수(name)에도 저장(재시작 후에도 유지). RENDER_API_KEY 없으면 None."""
    api_key, sid = os.environ.get("RENDER_API_KEY"), os.environ.get("RENDER_SERVICE_ID")
    if not (api_key and sid):
        return None, "Render API 키 미연결 — 서버가 재시작·재배포되면 이 값이 사라질 수 있습니다(Render 무료 플랜)"
    import urllib.request
    url = f"https://api.render.com/v1/services/{sid}/env-vars/{name}"
    data = json.dumps({"value": value}).encode() if value is not None else None
    req = urllib.request.Request(url, data=data, method="PUT" if value is not None else "DELETE", headers={
        "Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return (200 <= r.status < 300), "Render 환경변수에도 저장했습니다(재시작 후에도 유지)"
    except Exception as e:
        return False, f"Render 환경변수 저장 실패: {type(e).__name__}: {e}"[:200]


def set_user_code(app, code: str | None, *, sync=render_sync) -> dict:
    """이용자 코드 변경(None이면 이용자 접속 끄기). 서버 파일에 해시로 저장하고 이용자 세션을 모두 끊는다."""
    if code is not None:
        code = code.strip()
        if len(code) < 6:
            return {"ok": False, "message": "이용자 코드는 6자 이상이어야 합니다."}
        if check_code(app, code, "admin"):
            return {"ok": False, "message": "관리자 코드와 다른 코드를 정하세요."}
    rec = {"updated_at": iso()}
    rec.update({"user_code_hash": hash_password(code)} if code is not None else {"user_code_disabled": True})
    f = access_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec), encoding="utf-8")
    app["codes"] = [c for c in app["codes"] if c["role"] != "viewer"] + (
        [{"role": "viewer", "secret": rec["user_code_hash"]}] if code is not None else [])
    app["keys"] = role_keys(app["codes"])
    synced, sync_msg = sync(code)
    log("관리자 화면에서 이용자 코드 " + ("변경" if code is not None else "끄기"))
    return {"ok": True, "message": ("이용자 코드를 바꿨습니다. 기존 이용자 접속은 모두 끊겼습니다."
                                    if code is not None else "이용자 접속을 껐습니다."),
            "render_synced": synced, "sync_message": sync_msg}


def access_info(app) -> dict:
    acc = load_access()
    has_viewer = any(c["role"] == "viewer" for c in app["codes"])
    source = "dashboard" if acc else ("env" if os.environ.get("IPO_RADAR_USER_CODE") else None)
    return {"user_code_set": has_viewer, "source": source, "updated_at": acc.get("updated_at"),
            "render_sync": bool(os.environ.get("RENDER_API_KEY") and os.environ.get("RENDER_SERVICE_ID"))}


def view_name(index: str) -> str:
    return index[:-5] + "_view.html" if index.endswith(".html") else index + "_view"


# ---------- HTTP ----------

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def login_page(app, error: str = "", mode: str = "viewer") -> str:
    import html
    admin = mode == "admin"
    b = app.get("brand") or {}
    logo = f"<img src='{b['logo_data']}' alt='' style='height:28px'>" if b.get("logo_data") else "<b style='font-size:20px;color:#692c00'>IPO Radar</b>"
    branch = html.escape(b.get("branch") or "공모주 상담 데스크")
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    accent = "#231815" if admin else "#692c00"
    title = "관리자 모드 접속" if admin else "공모주 상담 데스크 접속"
    desc = ("관리자 코드를 입력하세요. 데이터 관리, 지금 갱신, 자료원 상태를 볼 수 있습니다." if admin
            else "부서에서 받은 이용자 코드를 입력하세요.")
    tab = lambda m, label: (f"<a href='/login?mode={m}' class='tab{' on' if mode == m else ''}' "  # noqa: E731
                            f"aria-current='{'page' if mode == m else 'false'}'>{label}</a>")
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IPO Radar 접속</title>
<style>
:root{{--ink:#231815;--sub:#8a7c72;--line:#e4ddd5;--brand:#692c00;--accent:{accent};--bg:#f6f4f1}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font-family:"Noto Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;padding:16px}}
.card{{width:min(400px,100%);background:#fff;border:1px solid var(--line);border-top:4px solid var(--accent);border-radius:6px;padding:24px;display:grid;gap:16px}}
.id{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}.id .br{{font-weight:800;font-size:18px}}
.tabs{{display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line);border-radius:4px;overflow:hidden}}
.tab{{text-align:center;padding:10px;font-size:14px;font-weight:500;color:var(--sub);text-decoration:none;background:#faf8f6}}
.tab.on{{background:var(--accent);color:#fff;font-weight:700}}
.tab:focus-visible{{outline:2px solid #b8873f;outline-offset:-2px}}
h1{{font-size:16px;margin:0}}p{{margin:0;font-size:13px;color:var(--sub);line-height:1.6}}
label{{font-size:13px;font-weight:700;display:grid;gap:6px}}
input{{font:inherit;font-size:16px;padding:10px 12px;border:1px solid #cfc4b8;border-radius:4px;letter-spacing:.1em}}
input:focus{{outline:2px solid #b8873f;outline-offset:1px}}
button{{font:inherit;font-weight:700;padding:11px;border:0;border-radius:4px;background:var(--accent);color:#fff;cursor:pointer;width:100%}}
.err{{color:#b3261e;font-weight:500}}
</style>
<div class="card">
  <div class="id">{logo}<span class="br">{branch}</span></div>
  <nav class="tabs" aria-label="접속 모드">{tab("viewer", "이용자")}{tab("admin", "관리자")}</nav>
  <h1>{title}</h1>
  <p>{desc}</p>
  {err}
  <form method="post" action="/login" autocomplete="off">
    <input type="hidden" name="mode" value="{'admin' if admin else 'viewer'}">
    <label>{'관리자 코드' if admin else '이용자 코드'}<input type="password" name="code" required autofocus></label>
    <div style="height:12px"></div><button type="submit">{'관리자 모드로 입장' if admin else '입장'}</button>
  </form>
</div></html>"""


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "IPORadar"
        sys_version = ""

        def log_message(self, fmt, *args):
            log(f"{self.client_address[0]} {fmt % args}")

        def _ip(self):
            return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip() \
                if app["trust_proxy"] else self.client_address[0]

        def _https(self):
            return app["trust_proxy"] and self.headers.get("X-Forwarded-Proto", "").lower() == "https"

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
                if isinstance(v, list):
                    for x in v:
                        self.send_header(k, x)
                else:
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _redirect(self, to, cookie=None):
            self._send(303, b"", extra={"Location": to, **({"Set-Cookie": cookie} if cookie else {})})

        def _cookie(self, value, max_age):
            c = f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
            return c + ("; Secure" if self._https() else "")

        def _role(self):
            if app["anon"]:
                return "admin"
            from http.cookies import SimpleCookie
            try:
                jar = SimpleCookie(self.headers.get("Cookie", ""))
                tok = jar[SESSION_COOKIE].value if SESSION_COOKIE in jar else None
            except Exception:
                tok = None
            return read_token(app, tok)

        def _need_login(self, path):
            if path.startswith("/api/") or path.startswith("/admin/") or path.endswith(".json"):
                return self._send(401, "로그인이 필요합니다.".encode())
            return self._redirect("/login")

        def _file(self, name, ctype):
            p = app["out"] / name
            if not p.is_file():
                return self._send(503, preparing_page(app).encode("utf-8"), "text/html; charset=utf-8")
            self._send(200, p.read_bytes(), ctype)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self._guard(self._get)

        def do_POST(self):
            self._guard(self._post)

        def _guard(self, fn):
            """처리 중 예외가 나도 연결을 끊지 않고 오류 화면을 돌려준다(끊으면 Render가 502를 보여 줌)."""
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                log("요청 처리 오류 " + self.path.split("?")[0] + "\n" + traceback.format_exc())
                try:
                    msg = f"서버 오류가 났습니다: {type(e).__name__}. 잠시 후 다시 시도하세요. 계속되면 관리자에게 알려 주세요."
                    self._send(500, msg.encode("utf-8"))
                except Exception:  # noqa: BLE001 — 이미 응답을 보내기 시작한 경우
                    pass

        def _get(self):
            path = self.path.split("?")[0]
            if path == "/healthz":
                return self._send(200, b"ok")
            if path == "/login":
                mode = "admin" if "mode=admin" in self.path else "viewer"
                err = ("관리자 코드가 맞지 않습니다." if mode == "admin" else "이용자 코드가 맞지 않습니다.") if "e=1" in self.path else ""
                return self._send(200, login_page(app, err, mode).encode("utf-8"), "text/html; charset=utf-8")
            if path == "/logout":
                return self._redirect("/login", self._cookie("", 0))
            role = self._role()
            if not role:
                return self._need_login(path)
            admin = role == "admin"
            if path in ("/", "/menu"):
                return self._send(200, hub_page(app, role).encode("utf-8"), "text/html; charset=utf-8")
            if path in ("/ipo", "/ipo/", "/index.html"):
                return self._file(app["index"] if admin else view_name(app["index"]), "text/html; charset=utf-8")
            if path in ("/macro", "/macro/"):
                return self._send(200, macro_page(app, role).encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/macro":
                from . import macro
                conn = connect()
                try:
                    if macro.needs_refresh(conn) and not macro.STATE["running"]:
                        macro.refresh_async("on-view")  # 오래된 자료면 백그라운드에서 갱신(화면은 지금 자료로 먼저 그림)
                    body = macro.build(conn)
                finally:
                    conn.close()
                body["role"] = role
                if not admin:  # 이용자에게는 오류 상세·키 이름을 보내지 않는다
                    for st in body["status"].values():
                        st.pop("message", None)
                        st.pop("key_set", None)
                    body["refresh"].pop("last_error", None)
                    body.pop("keys", None)
                return self._send(200, json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            if path == "/data.json":
                return self._file("data.json" if admin else "data_view.json", "application/json; charset=utf-8")
            if path == "/ipo_calendar.ics":
                return self._file("ipo_calendar.ics", "text/calendar; charset=utf-8")
            if path == "/ipo_list.csv":
                return self._file("ipo_list.csv", "text/csv; charset=utf-8")
            if path == "/api/status":
                st = app["runner"].state
                body = {"role": role, "user": "관리자" if admin else "이용자", "running": st["running"], "next": st.get("next")}
                if admin:
                    body.update(last=st.get("last"), last_error=st.get("last_error"), started_at=st.get("started_at"),
                                access=access_info(app))
                dj = app["out"] / "data.json"
                if dj.is_file():
                    try:
                        body["generated_at"] = json.loads(dj.read_text(encoding="utf-8"))["meta"]["generated_at"]
                    except Exception:
                        pass
                return self._send(200, json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            self._send(404, b"not found")

        def _post(self):
            path = self.path.split("?")[0]
            if path == "/login":
                ip = self._ip()
                if app["guard"].blocked(ip):
                    return self._send(429, login_page(app, "코드 입력 실패가 많아 15분간 잠겼습니다. 잠시 후 다시 시도하세요.").encode("utf-8"),
                                      "text/html; charset=utf-8")
                n = int(self.headers.get("Content-Length") or 0)
                from urllib.parse import parse_qs
                form = parse_qs(self.rfile.read(min(n, 4096)).decode("utf-8", "replace"))
                mode = "admin" if (form.get("mode") or ["viewer"])[0] == "admin" else "viewer"
                role = check_code(app, (form.get("code") or [""])[0], mode)
                if not role:
                    app["guard"].fail(ip)
                    return self._redirect(f"/login?mode={mode}&e=1")
                app["guard"].ok(ip)
                log(f"{ip} {'관리자' if role == 'admin' else '이용자'} 모드 접속")
                return self._redirect("/", self._cookie(make_token(app, role), SESSION_HOURS * 3600))
            role = self._role()
            if not role:
                return self._need_login(path)
            if path == "/admin/user-code":
                if role != "admin":
                    return self._send(403, "관리자만 바꿀 수 있습니다.".encode())
                if self.headers.get("X-Requested-With") != "ipo-radar":
                    return self._send(400, b"bad request")
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    req = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
                except ValueError:
                    return self._send(400, b"bad json")
                res = set_user_code(app, None if req.get("disable") else str(req.get("code") or ""))
                res["access"] = access_info(app)
                return self._send(200 if res["ok"] else 400, json.dumps(res, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            if path in ("/admin/macro-refresh", "/admin/macro-manual", "/admin/macro-key"):
                if role != "admin":
                    return self._send(403, "관리자만 할 수 있습니다.".encode())
                if self.headers.get("X-Requested-With") != "ipo-radar":
                    return self._send(400, b"bad request")
                from . import macro
                if path == "/admin/macro-refresh":
                    started = macro.refresh_async("manual-admin")
                    return self._send(202 if started else 409, json.dumps(
                        {"started": started, "message": "매크로 지표 갱신을 시작했습니다" if started else "이미 갱신 중입니다"},
                        ensure_ascii=False).encode(), "application/json; charset=utf-8")
                if path == "/admin/macro-key":
                    n = int(self.headers.get("Content-Length") or 0)
                    try:
                        req = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
                    except ValueError:
                        return self._send(400, b"bad json")
                    name = str(req.get("name") or "")
                    res = macro.set_key(name, None if req.get("clear") else str(req.get("key") or ""),
                                        sync=lambda v: render_sync(v, name))
                    if res["ok"]:
                        log(f"관리자 화면에서 {name} {'삭제' if req.get('clear') else '저장'}")
                        res["refresh_started"] = macro.refresh_async("key-saved")  # 저장 즉시 수집해 키가 맞는지 확인
                    res["keys"] = macro.key_info()
                    return self._send(200 if res["ok"] else 400, json.dumps(res, ensure_ascii=False).encode(),
                                      "application/json; charset=utf-8")
                n = int(self.headers.get("Content-Length") or 0)
                if n > 2_000_000:
                    return self._send(413, "입력이 너무 깁니다(2MB 이하).".encode())
                try:
                    req = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, b"bad json")
                conn = connect()
                try:
                    if req.get("delete"):
                        k = macro.delete_series(conn, str(req["delete"]))
                        res = {"ok": k > 0, "deleted": k, "message": f"{k}개 값을 지웠습니다" if k else "지울 관리자 입력 자료가 없습니다"}
                    else:
                        res = macro.save_manual(conn, str(req.get("text") or ""))
                        res["message"] = f"{res['saved']}행 저장" + (f", {len(res['errors'])}행 확인 필요" if res["errors"] else "")
                finally:
                    conn.close()
                return self._send(200 if res.get("saved") or res.get("ok") else 400,
                                  json.dumps(res, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            if path == "/admin/refresh":
                if role != "admin":
                    return self._send(403, "관리자만 수집을 실행할 수 있습니다.".encode())
                if self.headers.get("X-Requested-With") != "ipo-radar":  # CSRF 방지(SameSite 쿠키 + 같은 출처 스크립트 전용 헤더)
                    return self._send(400, b"bad request")
                started = app["runner"].run_async("manual-admin")
                return self._send(202 if started else 409, json.dumps(
                    {"started": started, "message": "수집을 시작했습니다" if started else "이미 수집 중입니다"},
                    ensure_ascii=False).encode(), "application/json; charset=utf-8")
            self._send(404, b"not found")
    return H


def _brand_bits(app):
    import html
    b = app.get("brand") or {}
    logo = f"<img src='{b['logo_data']}' alt='' style='height:26px'>" if b.get("logo_data") else "<b style='font-size:18px;color:#692c00'>IPO Radar</b>"
    return logo, html.escape(b.get("branch") or "공모주 상담 데스크")


def hub_page(app, role: str) -> str:
    """로그인 후 첫 화면: 들어갈 대시보드를 고른다."""
    import html
    logo, branch = _brand_bits(app)
    admin = role == "admin"
    ipo_at = macro_at = "아직 없음"
    dj = app["out"] / "data.json"
    if dj.is_file():
        try:
            ipo_at = json.loads(dj.read_text(encoding="utf-8"))["meta"]["generated_at"][:16].replace("T", " ")
        except Exception:
            pass
    try:
        conn = connect()
        try:
            row = conn.execute("SELECT MAX(checked_at) t FROM macro_status").fetchone()
            macro_at = (row["t"] or "아직 없음")[:16].replace("T", " ")
        finally:
            conn.close()
    except Exception:
        pass
    cards = [
        ("/ipo", "IPO Radar · 공모주", "이번 주 청약 캘린더, 기업 공부 노트·재무, 적정가 판단, 수요예측 결과, 증거금 계산기",
         f"마지막 수집 {ipo_at} (매주 월 08:00 자동)"),
        ("/macro", "매크로 분석", "한·미 금리와 국채 가격 변화, 신용 스프레드·CDS, 공포·탐욕 지수, DRAM 가격, 피자 지수, AI 토큰 사용량·비용",
         f"마지막 갱신 {macro_at} (6시간마다 자동)"),
    ]
    if admin:
        cards.append(("/ipo#sources", "데이터 관리 · 관리자", "자료원 상태, 지금 갱신, 이용자 코드 변경", "관리자 모드에서만 보입니다"))
    items = "".join(f"<a class='m' href='{h}'><b>{html.escape(t)}</b><span>{html.escape(d)}</span><em>{html.escape(f)}</em></a>"
                    for h, t, d, f in cards)
    chip = "<span class='chip adm'>관리자 모드</span>" if admin else "<span class='chip'>이용자 모드</span>"
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>종합 대시보드</title>
<style>
:root{{--ink:#231815;--sub:#6f6259;--line:#e4ddd5;--brand:#692c00;--bg:#f6f4f1}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:"Noto Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif}}
header{{background:#fff;border-bottom:1px solid var(--line);border-top:4px solid var(--brand)}}
.in{{max-width:980px;margin:0 auto;padding:14px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.br{{font-weight:800}}.sp{{flex:1}}
.chip{{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:3px 10px;color:var(--sub)}}
.chip.adm{{background:var(--ink);border-color:var(--ink);color:#fff}}
.out{{font-size:13px;color:var(--sub);text-decoration:none;border:1px solid var(--line);border-radius:4px;padding:6px 10px;background:#fff}}
main{{max-width:980px;margin:0 auto;padding:24px 16px}}
h1{{font-size:20px;margin:0 0 4px}}p.lead{{margin:0 0 20px;color:var(--sub);font-size:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}
a.m{{display:grid;gap:8px;align-content:start;background:#fff;border:1px solid var(--line);border-left:4px solid var(--brand);border-radius:6px;
padding:18px;color:inherit;text-decoration:none;min-height:150px}}
a.m:hover{{border-color:var(--brand)}}a.m:focus-visible{{outline:2px solid #b8873f;outline-offset:2px}}
a.m b{{font-size:17px}}a.m span{{font-size:14px;line-height:1.6;color:var(--sub)}}a.m em{{font-style:normal;font-size:12px;color:var(--sub);margin-top:auto}}
</style>
<header><div class="in">{logo}<span class="br">{branch}</span><span class="sp"></span>{chip}<a class="out" href="/logout">로그아웃</a></div></header>
<main><h1>종합 대시보드</h1><p class="lead">볼 화면을 고르세요. 각 화면 위쪽의 ‘← 메뉴’로 이 화면에 돌아옵니다.</p>
<nav class="grid" aria-label="대시보드 메뉴">{items}</nav></main></html>"""


def macro_page(app, role: str) -> str:
    logo, branch = _brand_bits(app)
    tpl = (config.DASHBOARD_DIR / "macro.html").read_text(encoding="utf-8")
    boot = json.dumps({"role": role, "branch": branch, "logo": logo}, ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("/*__MACRO_BOOT__*/null", boot)


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


def _codes_from_env() -> list[dict]:
    codes = []
    admin = os.environ.get("IPO_RADAR_ADMIN_CODE") or config.SERVER_ADMIN_PASSWORD  # 예전 설정(관리자 비밀번호)도 관리자 코드로 인정
    user = os.environ.get("IPO_RADAR_USER_CODE", "")
    acc = load_access()  # 관리자 화면에서 정한 이용자 코드(해시)가 환경변수보다 우선
    if acc.get("user_code_hash"):
        user = acc["user_code_hash"]
    elif acc.get("user_code_disabled"):
        user = ""
    if admin:
        codes.append({"role": "admin", "secret": admin})
    if user:
        codes.append({"role": "viewer", "secret": user})
    for name, u in parse_users(config.SERVER_USERS).items():  # 해시로 등록한 코드
        codes.append({"role": "admin" if u["role"] == "admin" else "viewer", "secret": u["hash"]})
    return codes


def build_app(out_dir: Path | None = None, codes: list[dict] | None = None, anon: bool | None = None,
              with_sample: bool | None = None, trust_proxy: bool | None = None) -> dict:
    out = Path(out_dir or config.OUTPUT_DIR)
    codes = _codes_from_env() if codes is None else codes
    anon = config.SERVER_ALLOW_ANON if anon is None else anon
    plain = [c for c in codes if not c["secret"].startswith("pbkdf2_sha256$")]
    for c in plain:
        if len(c["secret"]) < (8 if c["role"] == "admin" else 6):
            raise SystemExit("접속 코드가 너무 짧습니다(관리자 8자 이상, 이용자 6자 이상).")
    if len({c["secret"] for c in plain}) < len(plain):
        raise SystemExit("관리자 코드와 이용자 코드는 서로 달라야 합니다.")
    if not any(c["role"] == "admin" for c in codes) and not anon:
        raise SystemExit("관리자 코드가 없습니다. IPO_RADAR_ADMIN_CODE(관리자)와 IPO_RADAR_USER_CODE(이용자)를 설정하세요.")
    brand_file = config.DATA_DIR / "brand.json"
    index, brand = "index.html", None
    if brand_file.exists() and config.SERVER_BRAND:
        brand = json.loads(brand_file.read_text(encoding="utf-8"))
        index = Path(brand.get("output", "branch.html")).name
        logo = config.ROOT / brand.get("logo", "")
        if brand.get("logo") and logo.is_file():
            brand["logo_data"] = "data:image/png;base64," + base64.b64encode(logo.read_bytes()).decode()
    return {"out": out, "codes": codes, "keys": role_keys(codes), "anon": anon, "guard": LoginGuard(), "index": index, "brand": brand,
            "runner": Runner(out, config.SERVER_WITH_SAMPLE if with_sample is None else with_sample),
            "trust_proxy": os.environ.get("IPO_RADAR_TRUST_PROXY", "1") == "1" if trust_proxy is None else trust_proxy}


def serve(host: str | None = None, port: int | None = None):
    app = build_app()
    if config.SERVER_SCHEDULER:
        threading.Thread(target=app["runner"].scheduler, daemon=True).start()
        from . import macro
        threading.Thread(target=macro.loop, daemon=True).start()  # 매크로 지표는 6시간마다
    srv = ThreadingHTTPServer((host or config.SERVER_HOST, port or config.SERVER_PORT), make_handler(app))
    roles = sorted({c["role"] for c in app["codes"]})
    log(f"IPO Radar 서버 시작 http://{srv.server_address[0]}:{srv.server_address[1]} · 접속 코드 {roles} · 첫 화면 {app['index']}")
    srv.serve_forever()
