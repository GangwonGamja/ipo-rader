"""기업 식별·등록과 값 변경 탐지.

- 기업은 고정 uid로 관리한다. DART 고유번호, IPOSTOCK 번호, 정규화된 이름을 모두 별칭으로 연결해
  어느 소스에서 먼저 발견되든 같은 기업으로 합쳐진다.
- 값(fact)이 바뀌면 changes 테이블에 변경 전후를 남긴다. 같은 값이 다시 들어오면 아무것도 기록하지 않는다.
- 공식 공시가 집계 사이트·뉴스보다 우선한다. 낮은 등급 소스가 다른 값을 주면 덮어쓰지 않고 '출처 불일치'로 남긴다.
"""
from __future__ import annotations

import re
import unicodedata

from .db import dumps, iso, loads, sha

SOURCE_RANK = {
    "official_disclosure": 5, "exchange": 5, "underwriter": 4, "manual": 3,
    "aggregator": 2, "news": 1, "websearch_summary": 1, "sample": 0,
}
VERIFICATION_BY_SOURCE = {
    "official_disclosure": "official", "exchange": "official", "underwriter": "official",
    "aggregator": "aggregator", "manual": "aggregator", "news": "websearch",
    "websearch_summary": "websearch", "sample": "unverified",
}
VERIFICATION_RANK = {"official": 3, "aggregator": 2, "websearch": 1, "unverified": 0}

SCHEDULE_KEYS = {
    "sched.demand_forecast": "수요예측",
    "sched.price_fix": "공모가 확정",
    "sched.subscription": "청약",
    "sched.payment": "납입",
    "sched.refund": "환불",
    "sched.listing": "상장예정",
}

_STRIP = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\s+|[·\.\,\-_'\"]")


def normalize_name(name: str) -> str:
    n = unicodedata.normalize("NFKC", name or "").strip()
    n = _STRIP.sub("", n)
    return n.lower()


def classify_kind(name: str) -> str:
    n = normalize_name(name)
    if "스팩" in n or "기업인수목적" in n or re.search(r"spac", n):
        return "spac"
    if "리츠" in n or "reit" in n or "부동산투자회사" in n:
        return "reits"
    return "general"


def _record_change(conn, run_id, uid, change_type, field=None, before=None, after=None,
                   source_id=None, at=None, dedupe=False):
    if dedupe:
        hit = conn.execute(
            "SELECT 1 FROM changes WHERE uid=? AND change_type=? AND IFNULL(field,'')=IFNULL(?,'') AND IFNULL(after,'')=IFNULL(?,'')",
            (uid, change_type, field, after)).fetchone()
        if hit:
            return
    conn.execute(
        "INSERT INTO changes(run_id,uid,change_type,field,before,after,source_id,detected_at) VALUES(?,?,?,?,?,?,?,?)",
        (run_id, uid, change_type, field, before, after, source_id, at or iso()))


def resolve_company(conn, name: str, *, corp_code: str | None = None, ext_ids: dict | None = None,
                    run_id: str | None = None, source_id: str | None = None, source_type: str = "aggregator",
                    is_sample: bool = False, at: str | None = None, **attrs) -> tuple[str, bool]:
    """이름·DART 고유번호·외부 ID로 기업을 찾고, 없으면 새로 만든다. (uid, 신규여부) 반환."""
    at = at or iso()
    keys = []
    if corp_code:
        keys.append(f"dart:{corp_code}")
    for k, v in (ext_ids or {}).items():
        if v:
            keys.append(f"{k}:{v}")
    norm = normalize_name(name)
    keys.append(norm)

    uid = None
    for k in keys:
        row = conn.execute("SELECT uid FROM company_aliases WHERE alias=?", (k,)).fetchone()
        if row:
            uid = row["uid"]
            break
    created = False
    if uid is None:
        uid = "C" + sha(("dart:" + corp_code) if corp_code else norm, 10)
        conn.execute(
            """INSERT INTO companies(uid,name,corp_code,kind,is_sample,first_seen,last_seen,verification)
               VALUES(?,?,?,?,?,?,?,?)""",
            (uid, name, corp_code, classify_kind(name), int(is_sample), at, at,
             VERIFICATION_BY_SOURCE.get(source_type, "unverified")))
        created = True
        _record_change(conn, run_id, uid, "new", None, None, name, source_id, at)
    for k in keys:
        conn.execute("INSERT OR IGNORE INTO company_aliases(alias,uid) VALUES(?,?)", (k, uid))

    row = conn.execute("SELECT * FROM companies WHERE uid=?", (uid,)).fetchone()
    updates = {"last_seen": at}
    if corp_code and not row["corp_code"]:
        updates["corp_code"] = corp_code
    ver = VERIFICATION_BY_SOURCE.get(source_type, "unverified")
    if VERIFICATION_RANK[ver] > VERIFICATION_RANK.get(row["verification"], 0):
        updates["verification"] = ver
    for k in ("market", "sector", "underwriters", "stock_code", "exclude_terms"):
        v = attrs.get(k)
        if v and row[k] != v and (not row[k] or SOURCE_RANK.get(source_type, 0) >= 2):
            updates[k] = v if isinstance(v, str) else dumps(v)
    sets = ",".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE companies SET {sets} WHERE uid=?", (*updates.values(), uid))
    return uid, created


def _source_rank(conn, source_id):
    if not source_id:
        return 0
    row = conn.execute("SELECT source_type FROM sources WHERE source_id=?", (source_id,)).fetchone()
    return SOURCE_RANK.get(row["source_type"], 0) if row else 0


def set_fact(conn, uid: str, key: str, value, *, source_id: str | None, run_id: str | None = None,
             unit: str | None = None, basis: str = "confirmed", as_of: str | None = None,
             at: str | None = None) -> str:
    """값을 저장하고 변경 여부를 반환: 'new' / 'changed' / 'same' / 'conflict'."""
    at = at or iso()
    new_v = dumps(value)
    row = conn.execute("SELECT * FROM facts WHERE uid=? AND key=?", (uid, key)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO facts(uid,key,value,unit,basis,source_id,as_of,collected_at) VALUES(?,?,?,?,?,?,?,?)",
            (uid, key, new_v, unit, basis, source_id, as_of, at))
        if key == "offer.price_final":
            _record_change(conn, run_id, uid, "price_fixed", key, None, new_v, source_id, at)
        elif key in SCHEDULE_KEYS and not _is_new_company_this_run(conn, uid, run_id):
            _record_change(conn, run_id, uid, "schedule_change", key, None, new_v, source_id, at)
        return "new"
    if row["value"] == new_v:
        # 같은 값: 수집 시각만 갱신 (더 높은 등급 소스면 출처도 교체)
        if _source_rank(conn, source_id) > _source_rank(conn, row["source_id"]):
            conn.execute("UPDATE facts SET source_id=?, as_of=?, collected_at=? WHERE uid=? AND key=?",
                         (source_id, as_of, at, uid, key))
        else:
            conn.execute("UPDATE facts SET collected_at=? WHERE uid=? AND key=? AND source_id IS ?",
                         (at, uid, key, source_id))
        return "same"
    if _source_rank(conn, source_id) < _source_rank(conn, row["source_id"]):
        _record_change(conn, run_id, uid, "source_conflict", key, row["value"], new_v, source_id, at, dedupe=True)
        return "conflict"
    conn.execute(
        "UPDATE facts SET value=?,unit=?,basis=?,source_id=?,as_of=?,collected_at=? WHERE uid=? AND key=?",
        (new_v, unit, basis, source_id, as_of, at, uid, key))
    if key in SCHEDULE_KEYS:
        ctype = "schedule_change"
    elif key == "offer.price_final":
        ctype = "price_fixed"
    else:
        ctype = "fact_change"
    _record_change(conn, run_id, uid, ctype, key, row["value"], new_v, source_id, at)
    return "changed"


def _is_new_company_this_run(conn, uid, run_id):
    if run_id is None:
        return False
    return conn.execute("SELECT 1 FROM changes WHERE uid=? AND change_type='new' AND run_id=?",
                        (uid, run_id)).fetchone() is not None


def set_status(conn, uid: str, status: str, *, run_id=None, source_id=None, note=None, at=None):
    """철회(withdrawn)·연기(postponed)·상장(listed)·정정(amendment) 처리."""
    at = at or iso()
    if status == "amendment":
        _record_change(conn, run_id, uid, "amendment", "filing", None, note, source_id, at, dedupe=True)
        return
    row = conn.execute("SELECT status FROM companies WHERE uid=?", (uid,)).fetchone()
    if row and row["status"] != status:
        conn.execute("UPDATE companies SET status=? WHERE uid=?", (status, uid))
        _record_change(conn, run_id, uid, status if status in ("withdrawn", "postponed") else "fact_change",
                       "status", row["status"], status, source_id, at)


def get_facts(conn, uid: str) -> dict:
    out = {}
    for r in conn.execute(
            """SELECT f.*, s.url, s.title AS src_title, s.publisher, s.source_type
               FROM facts f LEFT JOIN sources s ON s.source_id=f.source_id WHERE f.uid=?""", (uid,)):
        out[r["key"]] = {
            "value": loads(r["value"]), "unit": r["unit"], "basis": r["basis"], "as_of": r["as_of"],
            "collected_at": r["collected_at"],
            "source": {"url": r["url"], "title": r["src_title"], "publisher": r["publisher"],
                       "type": r["source_type"]} if r["url"] else None,
        }
    return out
