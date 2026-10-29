"""Claude API로 기업별 'AI 요약'과 '주간 AI 브리핑'을 만든다.

- ANTHROPIC_API_KEY가 없으면 아무것도 하지 않고 '설정 필요'로 표시한다.
- 수집된 사실(facts)과 계산 결과만 입력으로 준다. 모델에는 입력에 없는 사실을 쓰지 말라고 지시하고,
  각 문장에 근거 키(예: fin.annual)를 붙이게 해 화면에서 출처를 추적할 수 있게 한다.
- 입력 내용의 해시가 바뀐 경우에만 다시 생성한다 → 같은 주에 여러 번 실행해도 비용이 늘지 않는다.
- 한 번 실행에 생성하는 기업 수에 상한(IPO_RADAR_AI_MAX)을 둔다.
"""
from __future__ import annotations

import json
import os

from . import config
from .db import dumps, iso, loads, sha

MODEL = os.environ.get("IPO_RADAR_AI_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("IPO_RADAR_AI_EFFORT", "medium")
MAX_PER_RUN = int(os.environ.get("IPO_RADAR_AI_MAX", "20"))
# 서버 측 거절 대체(fallbacks "default")를 받는 모델. 그 밖의 모델(예: claude-haiku-5-5)에는 보내지 않는다.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
# 무료 플랜 등에서 서버가 깨어날 때마다 하는 '따라잡기' 수집에서도 AI를 돌릴지(기본: 돌리지 않음 → 비용 반복 방지)
AI_ON_CATCHUP = os.environ.get("IPO_RADAR_AI_ON_CATCHUP", "0") == "1"

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS ai_notes (
    uid TEXT NOT NULL,            -- 기업 uid 또는 '__weekly__'
    kind TEXT NOT NULL,           -- company / weekly
    input_hash TEXT NOT NULL,
    content TEXT NOT NULL,        -- JSON
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (uid, kind)
);
"""

SYSTEM = """당신은 증권사 지점 직원이 공모주(IPO)를 공부하고 고객 상담을 준비하도록 돕는 리서치 보조입니다.
반드시 지킬 것:
- 입력 JSON에 있는 사실과 계산 결과만 사용합니다. 입력에 없는 수치·사실·전망은 쓰지 않습니다. 모르면 "자료 없음"이라고 씁니다.
- 각 항목의 evidence에는 근거가 된 입력 키(예: fin.annual, offer.price_band, valuation, demand)를 적습니다.
- 확인된 실적, 회사 전망, 보도 내용, 분석자 추정을 구분해 표현합니다(예: "회사 측 전망에 따르면", "보도에 따르면").
- 매수·매도 권유, 수익 보장, 주가 예측 표현을 쓰지 않습니다.
- 한국어로, 짧고 쉬운 문장으로 씁니다."""

ITEM = {"type": "object", "properties": {"text": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
        "required": ["text", "evidence"], "additionalProperties": False}
COMPANY_SCHEMA = {
    "type": "object",
    "properties": {
        "one_liner": {"type": "string"},
        "business": {"type": "string"},
        "watch_points": {"type": "array", "items": ITEM},
        "risks": {"type": "array", "items": ITEM},
        "valuation_view": ITEM,
        "talking_points": {"type": "array", "items": ITEM},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["one_liner", "business", "watch_points", "risks", "valuation_view", "talking_points", "data_gaps"],
    "additionalProperties": False,
}
WEEKLY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "highlights": {"type": "array", "items": ITEM},
        "cautions": {"type": "array", "items": ITEM},
    },
    "required": ["headline", "summary", "highlights", "cautions"],
    "additionalProperties": False,
}


class NotConfigured(Exception):
    pass


def make_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise NotConfigured("ANTHROPIC_API_KEY 미설정")
    import anthropic  # 키가 있을 때만 필요
    return anthropic.Anthropic()


def _call(client, payload: dict, schema: dict, instruction: str) -> dict:
    """구조화 출력(JSON)으로 한 번 호출. 안전 분류기 거절 시 서버 측 대체 모델로 자동 재시도(fallbacks)."""
    kw = dict(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": f"{instruction}\n\n<input>\n{json.dumps(payload, ensure_ascii=False, default=str)}\n</input>"}],
    )
    if MODEL in FALLBACK_MODELS:
        kw.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    resp = client.beta.messages.create(**kw)
    if resp.stop_reason == "refusal":
        raise RuntimeError("모델이 요청을 거절했습니다")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("응답이 길이 제한에서 잘렸습니다")
    text = next(b.text for b in resp.content if b.type == "text")
    out = json.loads(text)
    out["_model"] = getattr(resp, "model", MODEL)
    return out


def company_payload(content: dict) -> dict:
    """보고서에서 모델에 줄 입력만 추린다(수집 시각 등 변동 값 제외 → 해시 안정)."""
    facts = {k: {"value": v.get("value"), "basis": v.get("basis"), "source": (v.get("source") or {}).get("publisher")}
             for k, v in (content.get("facts") or {}).items()}
    val = content.get("valuation") or {}
    return {
        "company": {k: content["company"].get(k) for k in ("name", "sector", "market", "underwriters", "status")},
        "stage": content.get("stage"), "facts": facts,
        "valuation": {k: val.get(k) for k in ("verdict", "eff_discount", "fair_range", "actual_value", "underwriter_value",
                                              "forecast_dependency", "provisional", "reasons", "rule")},
        "demand": {k: (content.get("demand") or {}).get(k) for k in ("score", "grade", "competition_ratio", "lockup_ratio", "flags")},
        "rule_points": [p["text"] for p in content["summary"]["points"]],
        "rule_risks": [p["text"] for p in content["summary"]["risks"]],
    }


def refresh(conn, run, company_reports: dict[str, dict], briefing: dict | None) -> dict:
    """반환: {'generated': n, 'skipped': n, 'errors': [...]}"""
    conn.executescript(SCHEMA_DDL)
    client = make_client()
    gen = skipped = 0
    errors = []
    targets = sorted(company_reports.items(), key=lambda kv: kv[1].get("stage") not in ("subscription", "demand_forecast"))
    for uid, content in targets:
        payload = company_payload(content)
        h = sha(dumps(payload), 32)
        row = conn.execute("SELECT input_hash FROM ai_notes WHERE uid=? AND kind='company'", (uid,)).fetchone()
        if row and row["input_hash"] == h:
            skipped += 1
            continue
        if gen >= MAX_PER_RUN:
            skipped += 1
            continue
        try:
            out = _call(client, payload, COMPANY_SCHEMA,
                        "아래 공모 예정 기업 자료로 공부 노트를 작성하세요. watch_points·risks·talking_points는 각각 3개 이하, "
                        "근거가 부족하면 개수를 줄이고 data_gaps에 부족한 자료를 적으세요. talking_points는 고객 상담 때 설명할 문장입니다.")
        except Exception as e:  # 한 기업 실패가 전체를 멈추지 않게
            errors.append(f"{content['company']['name']}: {type(e).__name__}: {e}")
            continue
        conn.execute("""INSERT INTO ai_notes(uid,kind,input_hash,content,model,created_at) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(uid,kind) DO UPDATE SET input_hash=excluded.input_hash, content=excluded.content,
                          model=excluded.model, created_at=excluded.created_at""",
                     (uid, "company", h, dumps(out), out.get("_model", MODEL), run.at))
        conn.commit()
        gen += 1
    if briefing:
        payload = {k: briefing.get(k) for k in ("week_of", "picks", "new_companies", "schedule_changes", "price_fixed",
                                                "postponed_withdrawn", "new_risks")}
        h = sha(dumps(payload), 32)
        row = conn.execute("SELECT input_hash FROM ai_notes WHERE uid='__weekly__' AND kind='weekly'").fetchone()
        if not (row and row["input_hash"] == h):
            try:
                out = _call(client, payload, WEEKLY_SCHEMA,
                            "아래 이번 주 공모주 일정·변경 자료로 지점 직원용 주간 브리핑을 쓰세요. headline은 한 문장, summary는 3~4문장, "
                            "highlights와 cautions는 각각 4개 이하입니다.")
                conn.execute("""INSERT INTO ai_notes(uid,kind,input_hash,content,model,created_at) VALUES(?,?,?,?,?,?)
                                ON CONFLICT(uid,kind) DO UPDATE SET input_hash=excluded.input_hash, content=excluded.content,
                                  model=excluded.model, created_at=excluded.created_at""",
                             ("__weekly__", "weekly", h, dumps(out), out.get("_model", MODEL), run.at))
                gen += 1
            except Exception as e:
                errors.append(f"주간 브리핑: {type(e).__name__}: {e}")
    return {"generated": gen, "skipped": skipped, "errors": errors}


def load_notes(conn) -> dict:
    try:
        rows = conn.execute("SELECT * FROM ai_notes").fetchall()
    except Exception:
        return {}
    return {(r["uid"] if r["kind"] == "company" else "__weekly__"): {**loads(r["content"]), "_created_at": r["created_at"],
                                                                     "_model": r["model"]} for r in rows}
