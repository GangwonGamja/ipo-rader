"""매크로 지표 수집·저장·가공.

자동 수집(공식·무료 공개 API만 사용)
- 미국 금리·국채·신용 스프레드·VIX·S&P 500·달러지수·유가·반도체 생산자물가: FRED (St. Louis Fed). FRED_API_KEY 필요
- 한국 기준금리·국고채·원/달러: 한국은행 ECOS. ECOS_API_KEY 필요
- 암호화폐 공포·탐욕 지수: alternative.me 공개 API (키 불필요)

관리자 입력(무료 공개 API가 없거나 유료 자료)
- 기업 CDS 프리미엄, DRAM 현물·고정거래 가격, 펜타곤 피자 지수, CNN 공포·탐욕 지수 등
  → 관리자 화면에서 "시리즈,날짜,값" 줄을 붙여 넣으면 저장·그래프로 표시한다.

자체 산출
- 시장 심리 지수(0~100): VIX·하이일드 스프레드·S&P 500 125일 이동평균 괴리의 1년 백분위 평균. CNN 지수와 다른 자체 지표
- 국채 가격 변화(추정): 만기 고정 액면채를 가정해 금리 변화로 가격 변화율을 계산
"""
from __future__ import annotations

import csv
import io
import os
import re
import threading
import time
from datetime import date, datetime, timedelta

from . import config, http
from .db import iso

DDL = """
CREATE TABLE IF NOT EXISTS macro_points (
  series TEXT NOT NULL, date TEXT NOT NULL, value REAL NOT NULL,
  source TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY(series, date)
);
CREATE TABLE IF NOT EXISTS macro_status (
  source TEXT PRIMARY KEY, status TEXT NOT NULL, message TEXT, checked_at TEXT NOT NULL, points INTEGER
);
CREATE TABLE IF NOT EXISTS macro_calendar (
  date TEXT NOT NULL, name TEXT NOT NULL, release_id INTEGER, source TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY(date, name)
);
CREATE TABLE IF NOT EXISTS ai_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, provider TEXT, model TEXT, purpose TEXT,
  input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER
);
"""

HISTORY_DAYS = int(os.environ.get("IPO_RADAR_MACRO_DAYS", "3660"))  # 약 10년(그래프에서 연도별로 골라 봄)
REFRESH_HOURS = float(os.environ.get("IPO_RADAR_MACRO_HOURS", "6"))

# id: (그룹, 이름, 단위, 자료원, 원천 코드)
# 원천 코드: FRED 시리즈 ID / ECOS "통계표코드/주기/항목코드"
SERIES = {
    # 미국 금리·국채 (FRED)
    "us.ffr": ("us_rates", "미국 연방기금 실효금리", "%", "fred", "DFF"),
    "us.2y": ("us_rates", "미국 국채 2년", "%", "fred", "DGS2"),
    "us.10y": ("us_rates", "미국 국채 10년", "%", "fred", "DGS10"),
    "us.30y": ("us_rates", "미국 국채 30년", "%", "fred", "DGS30"),
    "us.10y2y": ("us_rates", "미국 장단기 금리차(10년−2년)", "%p", "fred", "T10Y2Y"),
    # 신용 위험 (FRED, ICE BofA 옵션조정 스프레드)
    "credit.hy": ("credit", "미국 하이일드 회사채 스프레드", "%p", "fred", "BAMLH0A0HYM2"),
    "credit.ig": ("credit", "미국 투자등급 회사채 스프레드", "%p", "fred", "BAMLC0A0CM"),
    # 위험 선호 (FRED)
    "risk.vix": ("risk", "VIX 변동성 지수", "pt", "fred", "VIXCLS"),
    "eq.sp500": ("risk", "S&P 500", "pt", "fred", "SP500"),
    "eq.nasdaq": ("risk", "나스닥 종합", "pt", "fred", "NASDAQCOM"),
    "eq.dow": ("risk", "다우존스 산업평균", "pt", "fred", "DJIA"),
    # 환율·원자재 (FRED)
    "fx.dxy_broad": ("fx", "달러 지수(무역가중, 광의)", "pt", "fred", "DTWEXBGS"),
    "cmd.wti": ("fx", "WTI 원유", "$/bbl", "fred", "DCOILWTICO"),
    # 반도체 (FRED, 월간)
    "semi.ppi": ("semi", "미국 반도체 생산자물가지수(월)", "지수", "fred", "PCU334413334413"),
    # 한국 (ECOS)
    "kr.base": ("kr_rates", "한국은행 기준금리", "%", "ecos", "722Y001/D/0101000"),
    "kr.3y": ("kr_rates", "국고채 3년", "%", "ecos", "817Y002/D/010200000"),
    "kr.10y": ("kr_rates", "국고채 10년", "%", "ecos", "817Y002/D/010210000"),
    "kr.30y": ("kr_rates", "국고채 30년", "%", "ecos", "817Y002/D/010230000"),
    "fx.usdkrw": ("fx", "원/달러 환율(매매기준율)", "원", "ecos", "731Y001/D/0000001"),
    # 심리 (alternative.me)
    "fng.crypto": ("sentiment", "암호화폐 공포·탐욕 지수", "0~100", "altme", "fng"),
}

SOURCES = {
    "fred": {"name": "FRED (미국 세인트루이스 연준)", "key": "FRED_API_KEY", "url": "https://fred.stlouisfed.org"},
    "ecos": {"name": "한국은행 ECOS", "key": "ECOS_API_KEY", "url": "https://ecos.bok.or.kr"},
    "altme": {"name": "alternative.me 암호화폐 공포·탐욕 지수", "key": None, "url": "https://alternative.me/crypto/fear-and-greed-index/"},
    "manual": {"name": "관리자 입력", "key": None, "url": None},
}

# 관리자 입력 시리즈 이름 접두어 → 그룹
MANUAL_GROUPS = {"cds": "cds", "dram": "dram", "pizza": "pizza", "fng": "sentiment", "custom": "custom"}
MANUAL_HELP = {
    "cds": "기업·국가 5년 CDS 프리미엄(bp). 예: cds.삼성전자,2026-10-29,32",
    "dram": "DRAM 가격($). 예: dram.DDR5 16Gb 현물,2026-10-29,6.10",
    "pizza": "펜타곤 피자 지수(자료원 표기 수치). 예: pizza.지수,2026-10-29,3",
    "fng": "CNN 공포·탐욕 지수 등(0~100). 예: fng.CNN,2026-10-29,45",
    "custom": "그 밖의 지표. 예: custom.금 현물($/oz),2026-10-29,2400",
}

_lock = threading.Lock()
STATE = {"running": False, "last": None, "last_error": None}


def ensure(conn):
    conn.executescript(DDL)


# ---------- API 키: 관리자 화면에서 입력 ----------

KEY_NAMES = ("FRED_API_KEY", "ECOS_API_KEY")
KEY_RE = re.compile(r"^[A-Za-z0-9]{8,100}$")


def key_file():
    from pathlib import Path

    from . import db
    return Path(db.DB_PATH).parent / "api_keys.json"


def _load_keys() -> dict:
    import json
    try:
        return json.loads(key_file().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def get_key(name: str) -> str:
    """관리자 화면에서 넣은 키가 있으면 그것을, 없으면 서버 환경변수를 쓴다."""
    return (_load_keys().get(name) or {}).get("value") or os.environ.get(name, "")


def key_info() -> list[dict]:
    """화면용: 키 값은 끝 4자리만 보낸다."""
    saved, out = _load_keys(), []
    for name in KEY_NAMES:
        rec = saved.get(name) or {}
        v = rec.get("value") or os.environ.get(name, "")
        out.append({"name": name, "set": bool(v), "tail": v[-4:] if v else None,
                    "source": "dashboard" if rec.get("value") else ("env" if os.environ.get(name) else None),
                    "updated_at": rec.get("updated_at"), "env_set": bool(os.environ.get(name))})
    return out


def set_key(name: str, value: str | None, sync=None) -> dict:
    """value=None 이면 화면에서 넣은 키를 지운다(서버 환경변수 값이 있으면 그것으로 돌아감)."""
    import json
    if name not in KEY_NAMES:
        return {"ok": False, "message": "바꿀 수 없는 키 이름입니다."}
    if value is not None:
        value = value.strip()
        if not KEY_RE.match(value):
            return {"ok": False, "message": "키 형식이 맞지 않습니다(영문·숫자 8~100자, 공백 없이)."}
    keys = _load_keys()
    if value is None:
        keys.pop(name, None)
    else:
        keys[name] = {"value": value, "updated_at": iso()}
    f = key_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 서버 계정만 읽기
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(keys, fh)
    synced, sync_msg = sync(value) if sync else (None, "")
    label = SOURCES["ecos" if name == "ECOS_API_KEY" else "fred"]["name"]
    msg = f"{label} 키를 저장했습니다. 바로 수집을 시작합니다." if value is not None else f"{label} 키를 지웠습니다."
    return {"ok": True, "message": msg, "render_synced": synced, "sync_message": sync_msg}


def _mask(msg: str) -> str:
    """오류 메시지에 들어간 API 키를 가린다(키는 URL에 들어간다)."""
    for name in KEY_NAMES:
        for v in (os.environ.get(name), (_load_keys().get(name) or {}).get("value")):
            if v:
                msg = msg.replace(v, "****")
    return re.sub(r"(api_key=)[^&\s]+", r"\1****", msg)


def _num(s) -> float | None:
    try:
        v = float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN 제외


# ---------- 원천별 수집 ----------

def fetch_fred(code: str, start: date, key: str) -> list[tuple[str, float]]:
    url = ("https://api.stlouisfed.org/fred/series/observations?"
           f"series_id={code}&api_key={key}&file_type=json&observation_start={start.isoformat()}")
    js = http.fetch_json(url, check_robots=False)  # 공식 API(키 인증) — 웹페이지 크롤링 아님
    out = []
    for o in js.get("observations") or []:
        v = _num(o.get("value"))  # 결측은 "." 로 온다
        if v is not None and o.get("date"):
            out.append((o["date"], v))
    return out


def fetch_ecos(code: str, start: date, end: date, key: str) -> list[tuple[str, float]]:
    stat, cycle, item = code.split("/")
    out, first, page = [], 1, 1000
    while True:
        url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/{first}/{first + page - 1}/{stat}/{cycle}/"
               f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}/{item}")
        js = http.fetch_json(url, check_robots=False)  # 공식 Open API
        if "StatisticSearch" not in js:
            res = js.get("RESULT") or {}
            if str(res.get("CODE", "")).startswith("INFO-200"):  # 해당 데이터 없음
                return out
            raise http.FetchError(f"ECOS 응답 오류 {res.get('CODE')}: {res.get('MESSAGE')}")
        body = js["StatisticSearch"]
        for r in body.get("row") or []:
            t, v = str(r.get("TIME", "")), _num(r.get("DATA_VALUE"))
            if v is None or len(t) != 8:
                continue
            out.append((f"{t[:4]}-{t[4:6]}-{t[6:]}", v))
        total = int(_num(body.get("list_total_count")) or 0)
        first += page
        if first > total or first > 20000:
            return out


def fetch_altme(days: int) -> list[tuple[str, float]]:
    js = http.fetch_json(f"https://api.alternative.me/fng/?limit={days}&format=json")
    out = []
    for r in js.get("data") or []:
        v, ts = _num(r.get("value")), r.get("timestamp")
        if v is None or not ts:
            continue
        out.append((datetime.fromtimestamp(int(ts), config.KST).date().isoformat(), v))
    return out


def _upsert(conn, sid: str, pts, source: str, now: str) -> int:
    conn.executemany("""INSERT INTO macro_points(series,date,value,source,fetched_at) VALUES(?,?,?,?,?)
                        ON CONFLICT(series,date) DO UPDATE SET value=excluded.value, source=excluded.source,
                          fetched_at=excluded.fetched_at""", [(sid, d, v, source, now) for d, v in pts])
    return len(pts)


def _status(conn, source, status, message, n=None):
    conn.execute("""INSERT INTO macro_status(source,status,message,checked_at,points) VALUES(?,?,?,?,?)
                    ON CONFLICT(source) DO UPDATE SET status=excluded.status, message=excluded.message,
                      checked_at=excluded.checked_at, points=excluded.points""",
                 (source, status, _mask(message or "")[:400], iso(), n))


def collect(conn, today: date | None = None) -> dict:
    """모든 자동 수집 원천을 돈다. 한 시리즈 실패가 다른 시리즈를 막지 않는다."""
    ensure(conn)
    http.reset_run_state()
    today = today or datetime.now(config.KST).date()
    now = iso()
    summary = {}
    for src in ("fred", "ecos", "altme"):
        key_name = SOURCES[src]["key"]
        key = get_key(key_name) if key_name else ""
        ids = [sid for sid, s in SERIES.items() if s[3] == src]
        if key_name and not key:
            _status(conn, src, "not_configured", f"{key_name} 미등록 — 매크로 화면 'API 키·자료 관리' 탭(관리자 모드)에서 등록하면 자동 수집합니다")
            summary[src] = "not_configured"
            continue
        n, errs = 0, []
        for sid in ids:
            code = SERIES[sid][4]
            # 이미 있는 자료는 최근 45일만 다시 받는다(수정치 반영)
            rng = conn.execute("SELECT MIN(date) a, MAX(date) b FROM macro_points WHERE series=?", (sid,)).fetchone()
            first, last = rng["a"], rng["b"]
            full = today - timedelta(days=HISTORY_DAYS)
            if not last or date.fromisoformat(first) > full + timedelta(days=60):
                start = full  # 처음이거나 과거 자료가 모자라면 전체 기간을 받는다
            else:
                start = date.fromisoformat(last) - timedelta(days=45)
            try:
                if src == "fred":
                    pts = fetch_fred(code, start, key)
                elif src == "ecos":
                    pts = fetch_ecos(code, start, today, key)
                else:
                    pts = fetch_altme(min(HISTORY_DAYS, (today - start).days + 2))
                n += _upsert(conn, sid, pts, src, now)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{SERIES[sid][1]}: {type(e).__name__}: {e}")
        if src == "fred":  # 미국 경제지표 발표 일정(FRED 공표일 기준)
            try:
                k = save_releases(conn, fetch_fred_releases(key, today), now)
                n += k
            except Exception as e:  # noqa: BLE001
                errs.append(f"경제지표 발표 일정: {type(e).__name__}: {e}")
        status = "ok" if not errs else ("partial" if n else ("blocked" if any("거부" in x or "robots" in x for x in errs) else "error"))
        _status(conn, src, status, "; ".join(errs[:4]) if errs else f"{len(ids)}개 지표 갱신", n)
        conn.commit()
        summary[src] = status
    return summary


def refresh(trigger: str = "manual") -> dict | None:
    """서버에서 호출. 동시에 두 번 돌지 않게 잠근다."""
    if not _lock.acquire(blocking=False):
        return None
    from .db import connect
    STATE.update(running=True)
    try:
        conn = connect()
        try:
            res = collect(conn)
        finally:
            conn.close()
        STATE.update(last={"at": iso(), "trigger": trigger, "result": res}, last_error=None)
        return res
    except Exception as e:  # noqa: BLE001
        STATE["last_error"] = _mask(f"{type(e).__name__}: {e}")
        return None
    finally:
        STATE["running"] = False
        _lock.release()


def refresh_async(trigger: str = "manual") -> bool:
    if STATE["running"]:
        return False
    threading.Thread(target=refresh, args=(trigger,), daemon=True).start()
    return True


def needs_refresh(conn) -> bool:
    ensure(conn)
    row = conn.execute("SELECT MAX(checked_at) t FROM macro_status WHERE source!='manual'").fetchone()
    if not row or not row["t"]:
        return True
    return datetime.now(config.KST) - datetime.fromisoformat(row["t"]) > timedelta(hours=REFRESH_HOURS)


def loop():
    """서버 보조 스레드: 시작 직후 한 번, 이후 REFRESH_HOURS마다 갱신."""
    from .db import connect
    while True:
        try:
            conn = connect()
            due = needs_refresh(conn)
            conn.close()
        except Exception:  # noqa: BLE001
            due = True
        if due:
            refresh("schedule")
        time.sleep(600)


# ---------- 관리자 입력 ----------

SERIES_RE = re.compile(r"^(cds|dram|pizza|fng|custom)\.[^,\n]{1,60}$")


def parse_manual(text: str) -> tuple[list[tuple[str, str, float]], list[str]]:
    """'시리즈,날짜,값' 줄을 읽는다. 원본에 없는 값은 만들지 않고, 읽지 못한 줄은 사유와 함께 돌려준다."""
    rows, errs = [], []
    for i, rec in enumerate(csv.reader(io.StringIO(text or "")), 1):
        if not rec or all(not x.strip() for x in rec) or rec[0].strip().startswith("#"):
            continue
        if len(rec) != 3:
            errs.append(f"{i}행: 칸이 3개(시리즈,날짜,값)가 아닙니다")
            continue
        sid, d, v = (x.strip() for x in rec)
        if i == 1 and sid in ("시리즈", "series"):
            continue  # 머리글 줄
        if not SERIES_RE.match(sid):
            errs.append(f"{i}행: 시리즈 이름은 cds./dram./pizza./fng./custom. 으로 시작해야 합니다 ({sid[:30]})")
            continue
        try:
            d = date.fromisoformat(d.replace("/", "-").replace(".", "-")).isoformat()
        except ValueError:
            errs.append(f"{i}행: 날짜 형식은 YYYY-MM-DD 입니다 ({d[:20]})")
            continue
        val = _num(v)
        if val is None:
            errs.append(f"{i}행: 값이 숫자가 아닙니다 ({v[:20]})")
            continue
        rows.append((sid, d, val))
    return rows, errs


def save_manual(conn, text: str) -> dict:
    ensure(conn)
    rows, errs = parse_manual(text)
    if len(rows) > 20000:
        return {"ok": False, "saved": 0, "errors": ["한 번에 20,000행까지 넣을 수 있습니다"]}
    now = iso()
    conn.executemany("""INSERT INTO macro_points(series,date,value,source,fetched_at) VALUES(?,?,?,'manual',?)
                        ON CONFLICT(series,date) DO UPDATE SET value=excluded.value, source='manual', fetched_at=excluded.fetched_at""",
                     [(s, d, v, now) for s, d, v in rows])
    if rows:
        _status(conn, "manual", "ok", f"최근 입력 {len(rows)}행", len(rows))
    conn.commit()
    return {"ok": bool(rows) and not errs, "saved": len(rows), "errors": errs[:20]}


def delete_series(conn, sid: str) -> int:
    ensure(conn)
    if not SERIES_RE.match(sid or ""):
        return 0  # 자동 수집 시리즈는 지우지 않는다
    n = conn.execute("DELETE FROM macro_points WHERE series=? AND source='manual'", (sid,)).rowcount
    conn.commit()
    return n


# ---------- AI 토큰 사용량 ----------

def record_usage(conn, provider: str, model: str, purpose: str, usage: dict | None):
    if not usage:
        return
    ensure(conn)
    i, o = int(usage.get("input") or 0), int(usage.get("output") or 0)
    conn.execute("INSERT INTO ai_usage(at,provider,model,purpose,input_tokens,output_tokens,total_tokens) VALUES(?,?,?,?,?,?,?)",
                 (iso(), provider, model, purpose, i, o, int(usage.get("total") or i + o)))


def _price(name: str) -> float | None:
    return _num(os.environ.get(name)) if os.environ.get(name) else None


def usage_summary(conn, days: int = 90) -> dict:
    ensure(conn)
    since = (datetime.now(config.KST) - timedelta(days=days)).isoformat()
    rows = conn.execute("""SELECT substr(at,1,10) d, model, COUNT(*) calls, SUM(input_tokens) i, SUM(output_tokens) o, SUM(total_tokens) t
                           FROM ai_usage WHERE at>=? GROUP BY d, model ORDER BY d""", (since,)).fetchall()
    p_in, p_out = _price("IPO_RADAR_AI_PRICE_IN"), _price("IPO_RADAR_AI_PRICE_OUT")  # USD / 100만 토큰
    priced = p_in is not None and p_out is not None
    daily, models = {}, {}
    for r in rows:
        d = daily.setdefault(r["d"], {"date": r["d"], "calls": 0, "input": 0, "output": 0, "total": 0})
        for k, v in (("calls", r["calls"]), ("input", r["i"] or 0), ("output", r["o"] or 0), ("total", r["t"] or 0)):
            d[k] += v
        m = models.setdefault(r["model"] or "?", {"model": r["model"] or "?", "calls": 0, "input": 0, "output": 0})
        m["calls"] += r["calls"]
        m["input"] += r["i"] or 0
        m["output"] += r["o"] or 0
    tot = {k: sum(d[k] for d in daily.values()) for k in ("calls", "input", "output", "total")}
    cost = lambda i, o: round(i / 1e6 * p_in + o / 1e6 * p_out, 4) if priced else None  # noqa: E731
    for d in daily.values():
        d["cost_usd"] = cost(d["input"], d["output"])
    for m in models.values():
        m["cost_usd"] = cost(m["input"], m["output"])
    return {"days": days, "daily": list(daily.values()), "models": list(models.values()), "total": tot,
            "cost_usd": cost(tot["input"], tot["output"]), "priced": priced,
            "price": {"input": p_in, "output": p_out}, "provider": os.environ.get("IPO_RADAR_AI_PROVIDER", "gemini")}


# ---------- 가공 ----------

def par_bond_price(coupon: float, y: float, years: float) -> float:
    """액면 100, 반기 이표, 만기 years 인 채권의 가격(금리는 % 단위)."""
    c, r, n = coupon / 200, y / 200, int(round(years * 2))
    if n <= 0:
        return 100.0
    if abs(r) < 1e-12:
        return 100 * (c * n + 1)
    return 100 * (c * (1 - (1 + r) ** -n) / r + (1 + r) ** -n)


def bond_moves(points: list[tuple[str, float]], years: float) -> dict:
    """금리 시계열로 기간별 금리 변화(bp)와 추정 가격 변화율(%)을 계산한다."""
    if not points:
        return {}
    d_last, y_last = points[-1]
    last_day = date.fromisoformat(d_last)

    def at_or_before(target: date):
        best = None
        for d, v in points:
            if date.fromisoformat(d) <= target:
                best = (d, v)
            else:
                break
        return best

    out = {"date": d_last, "yield": y_last, "years": years, "moves": {}}
    for key, target in (("1d", None), ("1w", last_day - timedelta(days=7)), ("1m", last_day - timedelta(days=30)),
                        ("ytd", date(last_day.year - 1, 12, 31)), ("1y", last_day - timedelta(days=365))):
        base = points[-2] if key == "1d" and len(points) > 1 else (at_or_before(target) if target else None)
        if not base:
            continue
        y0 = base[1]
        out["moves"][key] = {"from": base[0], "bp": round((y_last - y0) * 100, 1),
                             "price_pct": round(par_bond_price(y0, y_last, years) - 100, 2)}
    return out


def _pct_rank(vals: list[float], v: float) -> float:
    if not vals:
        return 50.0
    below = sum(1 for x in vals if x < v)
    eq = sum(1 for x in vals if x == v)
    return 100 * (below + 0.5 * eq) / len(vals)


def sentiment_index(series: dict[str, list[tuple[str, float]]], window: int = 252) -> dict:
    """자체 시장 심리 지수. 구성요소별 최근 1년(약 252거래일) 백분위를 0~100으로 바꿔 평균한다.

    - VIX: 낮을수록 탐욕 → 100 − 백분위
    - 하이일드 스프레드: 좁을수록 탐욕 → 100 − 백분위
    - S&P 500의 125일 이동평균 대비 괴리: 클수록 탐욕 → 백분위
    구성요소가 2개 미만이면 산출하지 않는다(결측을 0으로 보지 않음).
    """
    comps = {}
    vix, hy, spx = series.get("risk.vix") or [], series.get("credit.hy") or [], series.get("eq.sp500") or []

    def daily_rank(pts, invert):
        res = {}
        vals = [v for _, v in pts]
        for i in range(len(pts)):
            if i < 60:
                continue
            hist = vals[max(0, i - window + 1):i + 1]
            r = _pct_rank(hist, vals[i])
            res[pts[i][0]] = 100 - r if invert else r
        return res

    if vix:
        comps["vix"] = daily_rank(vix, True)
    if hy:
        comps["hy"] = daily_rank(hy, True)
    if len(spx) > 125:
        gap = []
        for i in range(124, len(spx)):
            ma = sum(v for _, v in spx[i - 124:i + 1]) / 125
            gap.append((spx[i][0], spx[i][1] / ma - 1))
        comps["momentum"] = daily_rank(gap, False)
    dates = sorted(set().union(*[set(c) for c in comps.values()])) if comps else []
    hist, last_parts = [], {}
    for d in dates:
        parts = {k: c[d] for k, c in comps.items() if d in c}
        if len(parts) >= 2:
            hist.append((d, round(sum(parts.values()) / len(parts), 1)))
            last_parts = {k: round(v, 1) for k, v in parts.items()}
    if not hist:
        return {"available": False, "reason": "VIX·하이일드 스프레드·S&P 500 중 2개 이상이 필요합니다(FRED_API_KEY)"}
    v = hist[-1][1]
    label = "극단적 공포" if v < 25 else "공포" if v < 45 else "중립" if v <= 55 else "탐욕" if v <= 75 else "극단적 탐욕"
    return {"available": True, "date": hist[-1][0], "value": v, "label": label, "components": last_parts, "history": hist}


_SENTI_CACHE: dict = {}


def _version(conn) -> tuple:
    r = conn.execute("SELECT COUNT(*) n, MAX(fetched_at) f FROM macro_points").fetchone()
    return (r["n"], r["f"])


def _raw(conn) -> tuple[dict, dict]:
    rows = conn.execute("SELECT series, date, value, source FROM macro_points ORDER BY series, date").fetchall()
    raw: dict[str, list[tuple[str, float]]] = {}
    src: dict[str, str] = {}
    for r in rows:
        raw.setdefault(r["series"], []).append((r["date"], r["value"]))
        src[r["series"]] = r["source"]
    return raw, src


def _sentiment(conn, raw) -> dict:
    """계산이 무거워 자료가 바뀔 때만 다시 계산한다."""
    ver = _version(conn)
    if _SENTI_CACHE.get("ver") != ver:
        _SENTI_CACHE.update(ver=ver, val=sentiment_index(raw))
    return _SENTI_CACHE["val"]


def _change(pts, days):
    if not pts:
        return None
    last_d = date.fromisoformat(pts[-1][0])
    base = None
    for d, v in pts:
        if date.fromisoformat(d) <= last_d - timedelta(days=days):
            base = (d, v)
        else:
            break
    return {"from": base[0], "delta": round(pts[-1][1] - base[1], 6)} if base else None


def build(conn) -> dict:
    """화면 요약: 지표별 최근 값·변화·보유 연도(그래프 점은 points()로 따로 받는다)."""
    ensure(conn)
    raw, src = _raw(conn)
    series = {}
    for sid, pts in raw.items():
        if sid in SERIES:
            g, name, unit, source, code = SERIES[sid]
        else:
            prefix = sid.split(".", 1)[0]
            g, name, unit, source, code = MANUAL_GROUPS.get(prefix, "custom"), sid.split(".", 1)[1], "", "manual", None
        series[sid] = {"id": sid, "group": g, "name": name, "unit": unit, "source": src.get(sid, source), "code": code,
                       "last": pts[-1], "first": pts[0][0], "count": len(pts),
                       "years": sorted({d[:4] for d, _ in pts}, reverse=True),
                       "chg_1d": ({"from": pts[-2][0], "delta": round(pts[-1][1] - pts[-2][1], 6)} if len(pts) > 1 else None),
                       "chg_1w": _change(pts, 7), "chg_1m": _change(pts, 30)}
    for sid, (g, name, unit, source, code) in SERIES.items():  # 아직 자료가 없는 지표도 빈 상태로 보여 준다
        series.setdefault(sid, {"id": sid, "group": g, "name": name, "unit": unit, "source": source, "code": code,
                                "last": None, "first": None, "count": 0, "years": [], "chg_1d": None, "chg_1w": None, "chg_1m": None})
    status = {r["source"]: dict(r) for r in conn.execute("SELECT * FROM macro_status").fetchall()}
    for s, meta in SOURCES.items():
        st = status.setdefault(s, {"source": s, "status": "never", "message": "아직 수집하지 않았습니다", "checked_at": None, "points": None})
        st.update(name=meta["name"], key=meta["key"], url=meta["url"],
                  key_set=bool(get_key(meta["key"])) if meta["key"] else None)
    bonds = {k: bond_moves(raw.get(sid, []), yrs) for k, sid, yrs in
             (("us.2y", "us.2y", 2), ("us.10y", "us.10y", 10), ("us.30y", "us.30y", 30),
              ("kr.3y", "kr.3y", 3), ("kr.10y", "kr.10y", 10), ("kr.30y", "kr.30y", 30))}
    senti = {k: v for k, v in _sentiment(conn, raw).items() if k != "history"}
    if senti.get("available"):
        senti["years"] = sorted({d[:4] for d, _ in _SENTI_CACHE["val"]["history"]}, reverse=True)
    return {"generated_at": iso(), "series": series, "status": status, "bonds": bonds, "sentiment": senti,
            "usage": usage_summary(conn, 365), "manual_help": MANUAL_HELP, "keys": key_info(),
            "history_days": HISTORY_DAYS, "calendar": calendar(conn), "checks": morning_checks(raw),
            "refresh": {"running": STATE["running"], "last": STATE["last"], "last_error": STATE["last_error"],
                        "every_hours": REFRESH_HOURS}}


SENTIMENT_ID = "_sentiment"


def points(conn, ids: list[str]) -> dict:
    """그래프용 전체 점. '_sentiment'는 자체 심리 지수 이력."""
    ensure(conn)
    out = {}
    want = [i for i in ids if i != SENTIMENT_ID][:20]
    if want:
        q = ",".join("?" * len(want))
        for r in conn.execute(f"SELECT series, date, value FROM macro_points WHERE series IN ({q}) ORDER BY series, date", want):
            out.setdefault(r["series"], []).append((r["date"], r["value"]))
        for i in want:
            out.setdefault(i, [])
    if SENTIMENT_ID in ids:
        raw, _ = _raw(conn)
        out[SENTIMENT_ID] = _sentiment(conn, raw).get("history") or []
    return out


# ---------- 아침 브리핑: 경제 일정·체크 포인트 ----------

# FRED 공표 일정 중 시장이 많이 보는 미국 지표(영문 공표명 일부 → 한국어 이름). 목록에 없는 공표는 화면에 넣지 않는다.
MAJOR_RELEASES = [
    ("employment situation", "미국 고용보고서(비농업 고용·실업률)"),
    ("consumer price index", "미국 소비자물가(CPI)"),
    ("producer price index", "미국 생산자물가(PPI)"),
    ("gross domestic product", "미국 GDP"),
    ("personal income and outlays", "미국 개인소득·소비(PCE 물가)"),
    ("advance monthly sales for retail", "미국 소매판매"),
    ("job openings and labor turnover", "미국 구인·이직(JOLTS)"),
    ("unemployment insurance weekly claims", "미국 주간 신규 실업수당 청구"),
    ("industrial production and capacity utilization", "미국 산업생산"),
    ("university of michigan", "미시간대 소비자심리"),
    ("new residential construction", "미국 주택착공"),
    ("fomc press release", "FOMC 성명"),
]


def fetch_fred_releases(key: str, today: date) -> list[tuple[str, str, int]]:
    url = ("https://api.stlouisfed.org/fred/releases/dates?"
           f"api_key={key}&file_type=json&include_release_dates_with_no_data=true&sort_order=asc&limit=1000"
           f"&realtime_start={(today - timedelta(days=1)).isoformat()}&realtime_end={(today + timedelta(days=14)).isoformat()}")
    js = http.fetch_json(url, check_robots=False)
    out = []
    for r in js.get("release_dates") or []:
        name, d = str(r.get("release_name") or ""), str(r.get("date") or "")
        ko = next((k for en, k in MAJOR_RELEASES if en in name.lower()), None)
        if ko and len(d) == 10:
            out.append((d, ko, int(r.get("release_id") or 0)))
    return out


def save_releases(conn, rows, now: str) -> int:
    ensure(conn)
    if rows:
        lo, hi = min(r[0] for r in rows), max(r[0] for r in rows)
        conn.execute("DELETE FROM macro_calendar WHERE source='fred' AND date BETWEEN ? AND ?", (lo, hi))
    conn.executemany("INSERT OR REPLACE INTO macro_calendar(date,name,release_id,source,fetched_at) VALUES(?,?,?,'fred',?)",
                     [(d, n, rid, now) for d, n, rid in rows])
    return len(rows)


def load_events() -> list[dict]:
    """중앙은행 회의 등 저장소에 적어 둔 일정(출처·확인 여부 포함)."""
    import json
    try:
        return json.loads((config.DATA_DIR / "macro_events.json").read_text(encoding="utf-8")).get("events", [])
    except Exception:  # noqa: BLE001
        return []


def calendar(conn, today: date | None = None, days: int = 14) -> list[dict]:
    ensure(conn)
    today = today or datetime.now(config.KST).date()
    lo, hi = (today - timedelta(days=1)).isoformat(), (today + timedelta(days=days)).isoformat()
    out = [{"date": r["date"], "name": r["name"], "source": "FRED 공표 일정", "verified": True, "kind": "release"}
           for r in conn.execute("SELECT date, name FROM macro_calendar WHERE date BETWEEN ? AND ? ORDER BY date", (lo, hi))]
    hi_ev = (today + timedelta(days=60)).isoformat()  # 중앙은행 회의는 드물고 중요해 60일 앞까지
    for e in load_events():
        if lo <= str(e.get("date", "")) <= hi_ev:
            out.append({"date": e["date"], "name": e.get("name"), "source": e.get("source"), "url": e.get("url"),
                        "verified": bool(e.get("verified")), "note": e.get("note"), "kind": "event"})
    return sorted(out, key=lambda x: (x["date"], x["kind"] != "event"))


def morning_checks(raw: dict, today: date | None = None) -> list[dict]:
    """정해진 규칙으로 눈에 띄는 움직임만 골라 사실 문장으로 알린다(해석·전망 아님)."""
    out = []

    def last2(sid):
        pts = raw.get(sid) or []
        return (pts[-1], pts[-2]) if len(pts) >= 2 else (None, None)

    def last1(sid):
        pts = raw.get(sid) or []
        return pts[-1] if pts else None

    def add(level, sid, text, d):
        out.append({"level": level, "series": sid, "text": text, "date": d})

    for sid, name, lim in (("eq.sp500", "S&P 500", 1.5), ("eq.nasdaq", "나스닥", 2.0), ("eq.dow", "다우", 1.5)):
        a, b = last2(sid)
        if a and b and b[1]:
            pct = (a[1] / b[1] - 1) * 100
            if abs(pct) >= lim:
                add("warn", sid, f"{name} 전일 대비 {pct:+.2f}% (기준 ±{lim}% 이상)", a[0])
    a = last1("risk.vix")
    if a and a[1] >= 20:
        add("bad" if a[1] >= 30 else "warn", "risk.vix", f"VIX {a[1]:.1f} — {'30 이상(공포 구간으로 자주 언급)' if a[1] >= 30 else '20 이상(변동성 확대 구간으로 자주 언급)'}", a[0])
    for sid, name in (("us.10y", "미국 10년물"), ("kr.3y", "국고채 3년")):
        a, b = last2(sid)
        if a and b and abs(a[1] - b[1]) >= 0.10:
            add("warn", sid, f"{name} 금리 전일 대비 {(a[1] - b[1]) * 100:+.0f}bp (기준 ±10bp 이상)", a[0])
    a, b = last2("fx.usdkrw")
    if a and b and abs(a[1] - b[1]) >= 10:
        add("warn", "fx.usdkrw", f"원/달러 전일 대비 {a[1] - b[1]:+.1f}원 (기준 ±10원 이상)", a[0])
    a = last1("us.10y2y")
    if a and a[1] < 0:
        add("info", "us.10y2y", f"미국 장단기 금리차 {a[1]:+.2f}%p — 역전 상태", a[0])
    hy = raw.get("credit.hy") or []
    if len(hy) > 5:
        ch = _change(hy, 7)
        if ch and ch["delta"] >= 0.30:
            add("warn", "credit.hy", f"미국 하이일드 스프레드 1주 {ch['delta'] * 100:+.0f}bp 확대 (기준 +30bp 이상)", hy[-1][0])
    a = last1("fng.crypto")
    if a and (a[1] <= 20 or a[1] >= 80):
        add("info", "fng.crypto", f"암호화폐 공포·탐욕 지수 {a[1]:.0f} — {'극단적 공포' if a[1] <= 20 else '극단적 탐욕'} 구간", a[0])
    return out
