"""네이버 데이터랩 검색어 트렌드 API — 개인 투자자 검색 관심 추이.

- NAVER_CLIENT_ID/SECRET 필요(네이버 개발자센터 애플리케이션에 '데이터랩(검색어트렌드)' API 추가).
- API는 요청마다 최댓값을 100으로 하는 상대값만 준다. 기업 간 비교가 가능하도록 모든 요청에
  기준 키워드 그룹 '공모주'를 함께 넣고, 각 기업 값을 기준 그룹의 최댓값 대비 비율로 다시 맞춘다.
- 한 요청에 키워드 그룹 최대 5개 → 기준 1 + 기업 4개씩 나눠 호출.
"""
from __future__ import annotations

from datetime import date, timedelta

from .. import config
from ..http import post_json

API = "https://openapi.naver.com/v1/datalab/search"
ANCHOR = {"groupName": "__anchor__", "keywords": ["공모주", "공모주 청약"]}


class NotConfigured(Exception):
    pass


def trends(names: list[str], today: date, days: int = 60) -> dict[str, dict[str, float]]:
    """기업명 → {날짜: 기준('공모주' 검색량 최댓값=100) 대비 지수}."""
    if not (config.NAVER_CLIENT_ID and config.NAVER_CLIENT_SECRET):
        raise NotConfigured("NAVER_CLIENT_ID/SECRET 미설정(데이터랩 검색어트렌드 권한 필요)")
    start, end = (today - timedelta(days=days)).isoformat(), today.isoformat()
    out: dict[str, dict[str, float]] = {}
    for i in range(0, len(names), 4):
        chunk = names[i:i + 4]
        body = {"startDate": start, "endDate": end, "timeUnit": "date",
                "keywordGroups": [ANCHOR] + [{"groupName": n, "keywords": [n, f"{n} 공모주", f"{n} 청약"]} for n in chunk]}
        d = post_json(API, body, check_robots=False, headers={
            "X-Naver-Client-Id": config.NAVER_CLIENT_ID, "X-Naver-Client-Secret": config.NAVER_CLIENT_SECRET})
        res = {r["title"]: {x["period"]: x["ratio"] for x in r.get("data", [])} for r in d.get("results", [])}
        anchor_max = max(res.get("__anchor__", {}).values() or [0]) or None
        for n in chunk:
            series = res.get(n, {})
            out[n] = {k: (v / anchor_max * 100 if anchor_max else None) for k, v in series.items()}
    return out
