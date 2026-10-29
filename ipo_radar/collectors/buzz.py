"""국내 뉴스·블로그·카페·커뮤니티 언급 수집.

- 네이버 검색 API (뉴스·블로그·카페): NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 필요
  (https://developers.naver.com 에서 '검색' API 신청, 일 25,000회). 조회수·댓글 수는 제공하지 않음.
- Google 뉴스 RSS: 키 불필요. 네이버 키가 없을 때 뉴스 보조 경로.
- 네이버 종목토론방: 상장 후 종목코드가 생겨야 존재 → 상장 전 기업은 '해당 없음'.
- YouTube Data API: 공모주 분석 영상과 조회·좋아요·댓글(collectors/youtube.py).
- 커뮤니티 게시판 직접 수집(디시인사이드 등)은 하지 않는다.
"""
from __future__ import annotations

import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from .. import config
from ..http import fetch, fetch_json


class NotConfigured(Exception):
    pass


_TAG = re.compile(r"<[^>]+>")


def _clean(s: str | None) -> str:
    return html.unescape(_TAG.sub("", s or "")).strip()


def naver(channel: str, query: str, display: int = 100) -> list[dict]:
    if not (config.NAVER_CLIENT_ID and config.NAVER_CLIENT_SECRET):
        raise NotConfigured("NAVER_CLIENT_ID/SECRET 미설정")
    ep = {"news": "news", "blog": "blog", "cafe": "cafearticle"}[channel]
    url = f"https://openapi.naver.com/v1/search/{ep}.json?" + urllib.parse.urlencode(
        {"query": query, "display": display, "sort": "date"})
    d = fetch_json(url, check_robots=False, headers={
        "X-Naver-Client-Id": config.NAVER_CLIENT_ID, "X-Naver-Client-Secret": config.NAVER_CLIENT_SECRET})
    out = []
    for it in d.get("items", []):
        if channel == "news":
            pub = parsedate_to_datetime(it["pubDate"]).isoformat() if it.get("pubDate") else None
            link = it.get("originallink") or it.get("link")
            publisher = urllib.parse.urlparse(link).netloc
        else:
            pd = it.get("postdate")
            pub = f"{pd[:4]}-{pd[4:6]}-{pd[6:8]}" if pd else None
            link = it.get("link")
            publisher = it.get("bloggername") or it.get("cafename") or urllib.parse.urlparse(link).netloc
        out.append({"channel": channel, "url": link, "title": _clean(it.get("title")),
                    "snippet": _clean(it.get("description")), "published_at": pub, "publisher": publisher,
                    "views": None, "comments": None, "likes": None})
    return out


def google_news(query: str) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    xml = fetch(url)
    root = ET.fromstring(xml)
    out = []
    for it in root.iter("item"):
        src = it.find("source")
        pub = it.findtext("pubDate")
        out.append({"channel": "news", "url": it.findtext("link"), "title": _clean(it.findtext("title")),
                    "snippet": _clean(it.findtext("description")),
                    "published_at": parsedate_to_datetime(pub).isoformat() if pub else None,
                    "publisher": src.text if src is not None else None,
                    "views": None, "comments": None, "likes": None})
    return out


def collect_for(name: str) -> tuple[list[dict], dict]:
    """반환: (언급 목록, 채널별 수집 상태). 실패한 채널은 상태에 기록하고 계속 진행."""
    query = f'"{name}" 공모'
    items, status = [], {}
    for ch in ("news", "blog", "cafe"):
        try:
            items += naver(ch, query)
            status[f"naver_{ch}"] = "ok"
        except NotConfigured as e:
            status[f"naver_{ch}"] = f"not_configured: {e}"
        except Exception as e:
            status[f"naver_{ch}"] = f"error: {e}"
    if not str(status.get("naver_news", "")).startswith("ok"):
        try:
            items += google_news(query)
            status["google_news"] = "ok"
        except Exception as e:
            status["google_news"] = f"error: {e}"
    status["naver_stock_board"] = "n/a: 상장 전 종목은 토론방 없음"
    from . import youtube
    from datetime import date
    try:
        items += youtube.videos_for(name, date.today())
        status["youtube"] = "ok"
    except youtube.NotConfigured as e:
        status["youtube"] = f"not_configured: {e}"
    except Exception as e:
        status["youtube"] = f"error: {e}"
    return items, status
