"""YouTube Data API v3 — 공모주 분석 영상 수와 반응(조회·좋아요·댓글).

- YOUTUBE_API_KEY 필요(Google Cloud 콘솔에서 YouTube Data API v3 사용 설정, 무료 할당량 10,000단위/일).
- search.list(100단위) 1회 + videos.list(1단위) 1회 = 기업당 약 101단위 → 주 1회 수십 개 기업 처리 가능.
- 영상은 mentions 테이블에 channel='video'로 저장되어 관심도의 '참여도'(조회·댓글·좋아요)에 실제 값이 들어간다.
"""
from __future__ import annotations

import urllib.parse
from datetime import date, timedelta

from .. import config
from ..http import fetch_json

SEARCH = "https://www.googleapis.com/youtube/v3/search?"
VIDEOS = "https://www.googleapis.com/youtube/v3/videos?"


class NotConfigured(Exception):
    pass


def videos_for(name: str, today: date, days: int = 60) -> list[dict]:
    if not config.YOUTUBE_API_KEY:
        raise NotConfigured("YOUTUBE_API_KEY 미설정")
    q = {"part": "snippet", "q": f"{name} 공모주", "type": "video", "maxResults": 50, "regionCode": "KR",
         "relevanceLanguage": "ko", "order": "date", "key": config.YOUTUBE_API_KEY,
         "publishedAfter": (today - timedelta(days=days)).isoformat() + "T00:00:00Z"}
    s = fetch_json(SEARCH + urllib.parse.urlencode(q), check_robots=False)
    items = {it["id"]["videoId"]: it["snippet"] for it in s.get("items", []) if it.get("id", {}).get("videoId")}
    if not items:
        return []
    v = fetch_json(VIDEOS + urllib.parse.urlencode({"part": "statistics", "id": ",".join(items), "key": config.YOUTUBE_API_KEY}),
                   check_robots=False)
    stats = {it["id"]: it.get("statistics", {}) for it in v.get("items", [])}
    out = []
    for vid, sn in items.items():
        st = stats.get(vid, {})
        num = lambda k: int(st[k]) if st.get(k) is not None else None  # noqa: E731
        out.append({"channel": "video", "url": f"https://www.youtube.com/watch?v={vid}", "title": sn.get("title"),
                    "snippet": sn.get("description", "")[:300], "published_at": sn.get("publishedAt"),
                    "publisher": sn.get("channelTitle"), "views": num("viewCount"), "likes": num("likeCount"),
                    "comments": num("commentCount")})
    return out
