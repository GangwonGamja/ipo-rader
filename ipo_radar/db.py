"""SQLite 저장소. 모든 쓰기는 upsert로 하여 재실행해도 중복이 생기지 않는다."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import DB_PATH, KST

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    uid TEXT PRIMARY KEY,              -- 내부 고유 식별자 (변하지 않음)
    name TEXT NOT NULL,
    corp_code TEXT,                    -- DART 고유번호 (확인 시)
    stock_code TEXT,
    market TEXT,                       -- KOSPI / KOSDAQ / KONEX / 미확인
    sector TEXT,
    kind TEXT NOT NULL DEFAULT 'general', -- general / spac / reits
    underwriters TEXT,
    status TEXT NOT NULL DEFAULT 'active', -- active / postponed / withdrawn / listed
    verification TEXT NOT NULL DEFAULT 'unverified', -- official / aggregator / websearch / unverified
    is_sample INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    exclude_terms TEXT                 -- 동명이인/관계사 제외어 (JSON 배열)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_companies_corp ON companies(corp_code) WHERE corp_code IS NOT NULL;

CREATE TABLE IF NOT EXISTS company_aliases (
    alias TEXT PRIMARY KEY,            -- 정규화된 이름 또는 'dart:<corp_code>' / 'ipostock:<id>'
    uid TEXT NOT NULL REFERENCES companies(uid)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    publisher TEXT,
    source_type TEXT NOT NULL,         -- official_disclosure / exchange / underwriter / aggregator / news / community / websearch_summary / manual / sample
    as_of TEXT,                        -- 자료 기준일
    collected_at TEXT NOT NULL         -- 수집 시각
);

-- 현재 값 (일정·공모 조건·재무 등). basis: confirmed / company_projection / analyst_estimate / news_reported
CREATE TABLE IF NOT EXISTS facts (
    uid TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,               -- JSON
    unit TEXT,
    basis TEXT NOT NULL DEFAULT 'confirmed',
    source_id TEXT,
    as_of TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (uid, key)
);

-- 변경 전후 기록 (삭제하지 않음)
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    uid TEXT NOT NULL,
    change_type TEXT NOT NULL,         -- new / schedule_change / price_fixed / amendment / withdrawn / postponed / fact_change / new_risk
    field TEXT,
    before TEXT,
    after TEXT,
    source_id TEXT,
    detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mentions (
    mention_id TEXT PRIMARY KEY,       -- 정규화 URL 해시
    uid TEXT NOT NULL,
    channel TEXT NOT NULL,             -- news / blog / cafe / community
    publisher TEXT,
    url TEXT NOT NULL,
    title TEXT,
    snippet TEXT,
    published_at TEXT,
    views INTEGER, comments INTEGER, likes INTEGER,
    dup_group TEXT,                    -- 같은 내용(전재·복사) 묶음
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    is_promo INTEGER NOT NULL DEFAULT 0,
    relevant INTEGER NOT NULL DEFAULT 1,
    exclusion_reason TEXT,
    sentiment TEXT,                    -- pos / neu / neg
    collected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interest_scores (
    uid TEXT NOT NULL,
    run_id TEXT NOT NULL,
    score REAL,                        -- NULL = 산출 보류
    detail TEXT NOT NULL,              -- JSON (구성요소·표본·신뢰도·산식)
    computed_at TEXT NOT NULL,
    PRIMARY KEY (uid, run_id)
);

CREATE TABLE IF NOT EXISTS valuations (
    uid TEXT NOT NULL,
    run_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    detail TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (uid, run_id)
);

CREATE TABLE IF NOT EXISTS reports (
    uid TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,             -- JSON
    content_hash TEXT NOT NULL,
    diff TEXT,                         -- 지난 보고서 대비 변경 (JSON)
    run_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (uid, version)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,             -- schedule / manual / test
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,              -- running / success / partial / failed
    stages TEXT                        -- JSON
);

CREATE TABLE IF NOT EXISTS source_status (
    source TEXT PRIMARY KEY,
    status TEXT NOT NULL,              -- ok / error / not_configured / disabled / blocked
    last_attempt TEXT,
    last_success TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS briefings (
    week_of TEXT PRIMARY KEY,          -- 해당 주 월요일 (KST)
    run_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 네이버 데이터랩 검색 지수('공모주' 검색량 최댓값=100 기준으로 맞춘 값)
CREATE TABLE IF NOT EXISTS search_trends (
    uid TEXT NOT NULL,
    day TEXT NOT NULL,
    value REAL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (uid, day)
);

CREATE TABLE IF NOT EXISTS watchlist (
    uid TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    note TEXT
);
"""


def now_kst() -> datetime:
    return datetime.now(KST)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_kst()).isoformat(timespec="seconds")


def sha(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def loads(s):
    return None if s is None else json.loads(s)


def upsert_source(conn, url: str, source_type: str, title: str | None = None,
                  publisher: str | None = None, as_of: str | None = None,
                  collected_at: str | None = None) -> str:
    sid = sha(f"{source_type}|{url}")
    conn.execute(
        """INSERT INTO sources(source_id,url,title,publisher,source_type,as_of,collected_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(source_id) DO UPDATE SET
             title=COALESCE(excluded.title,title), publisher=COALESCE(excluded.publisher,publisher),
             as_of=COALESCE(excluded.as_of,as_of), collected_at=excluded.collected_at""",
        (sid, url, title, publisher, source_type, as_of, collected_at or iso()),
    )
    return sid


def set_source_status(conn, source: str, ok: bool | None, error: str | None = None,
                      status: str | None = None, note: str | None = None, at: str | None = None):
    at = at or iso()
    row = conn.execute("SELECT * FROM source_status WHERE source=?", (source,)).fetchone()
    fails = (row["consecutive_failures"] if row else 0)
    if ok is True:
        st, fails, last_success, err = status or "ok", 0, at, None
    elif ok is False:
        st, fails = status or "error", fails + 1
        last_success, err = (row["last_success"] if row else None), error
    else:  # 상태만 기록 (not_configured / disabled / blocked)
        st, last_success, err = status, (row["last_success"] if row else None), error
    conn.execute(
        """INSERT INTO source_status(source,status,last_attempt,last_success,last_error,consecutive_failures,note)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(source) DO UPDATE SET status=excluded.status,last_attempt=excluded.last_attempt,
             last_success=excluded.last_success,last_error=excluded.last_error,
             consecutive_failures=excluded.consecutive_failures,note=COALESCE(excluded.note,note)""",
        (source, st, at, last_success, err, fails, note),
    )
