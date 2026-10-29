"""IPOSTOCK 월별 IPO 달력 수집 (후보 발견용).

URL: http://www.ipostock.co.kr/sub03/ipo04.asp?str1=<연>&str2=<월>
실행일이 속한 달부터 실행일+90일이 걸치는 모든 달을 조회한다.

주의: 이 세션에서는 ipostock.co.kr 접속이 네트워크 정책으로 막혀 있어 실제 페이지 구조로
파서를 검증하지 못했다. 파서는 '달력 칸(td) 안에 날짜 숫자 + [구분]기업명' 형태를 폭넓게
인식하도록 작성했고, 접속 허용 후 `python -m ipo_radar probe ipostock`로 실제 구조를 확인해야 한다.
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import date, timedelta
from html.parser import HTMLParser

from .. import config
from ..http import fetch

BASE = "http://www.ipostock.co.kr/sub03/ipo04.asp?str1={y}&str2={m}"

LABELS = [
    (re.compile(r"수요\s*예측|수요"), "sched.demand_forecast"),
    (re.compile(r"공모\s*청약|청약"), "sched.subscription"),
    (re.compile(r"환불"), "sched.refund"),
    (re.compile(r"납입"), "sched.payment"),
    (re.compile(r"신규\s*상장|상장"), "sched.listing"),
]
_LABELED = re.compile(r"^\s*[\[\(<【]?\s*(수요\s*예측|수요|공모\s*청약|청약|환불|납입|신규\s*상장|상장)\s*[\]\)>】:]?\s*(.+?)\s*$")
_SUFFIXED = re.compile(r"^\s*(.+?)\s*[\[\(]\s*(수요\s*예측|수요|청약|환불|납입|상장)\s*[\]\)]\s*$")


def months_to_scan(today: date, lookahead_days: int = config.LOOKAHEAD_DAYS) -> list[tuple[int, int]]:
    end = today + timedelta(days=lookahead_days)
    out, y, m = [], today.year, today.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_url(y: int, m: int) -> str:
    return BASE.format(y=y, m=m)


class _CellParser(HTMLParser):
    """td 단위로 텍스트 조각과 링크를 모은다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells: list[list[tuple[str, str | None]]] = []
        self._depth = 0
        self._cur: list[tuple[str, str | None]] | None = None
        self._href: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "td":
            self._depth += 1
            if self._depth == 1 or self._cur is None:
                self._cur = []
            else:  # 중첩 td는 같은 칸으로 본다
                pass
        elif tag == "a":
            self._href = a.get("href")
        elif tag == "br" and self._cur is not None:
            self._cur.append(("\n", None))

    def handle_endtag(self, tag):
        if tag == "td":
            self._depth = max(0, self._depth - 1)
            if self._depth == 0 and self._cur is not None:
                self.cells.append(self._cur)
                self._cur = None
        elif tag == "a":
            self._href = None

    def handle_data(self, data):
        if self._cur is not None and data.strip():
            self._cur.append((data.strip(), self._href))


def _ext_id(href: str | None) -> str | None:
    if not href:
        return None
    q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    for k in ("code", "no", "idx", "num", "seq"):
        if q.get(k):
            return q[k][0]
    return None


def _label_key(label: str) -> str | None:
    for rx, key in LABELS:
        if rx.search(label):
            return key
    return None


def parse_calendar(html: str, year: int, month: int) -> list[dict]:
    p = _CellParser()
    p.feed(html)
    hits: dict[tuple[str, str], dict] = {}
    for cell in p.cells:
        day = None
        for text, href in cell:
            if text == "\n":
                continue
            if re.fullmatch(r"\d{1,2}", text) and 1 <= int(text) <= 31:
                day = int(text)
                continue
            if day is None:
                continue
            m = _LABELED.match(text) or None
            name = key = None
            if m:
                key, name = _label_key(m.group(1)), m.group(2)
            else:
                m2 = _SUFFIXED.match(text)
                if m2:
                    name, key = m2.group(1), _label_key(m2.group(2))
            if not (name and key):
                continue
            name = re.sub(r"\s+", " ", name).strip(" -·")
            if len(name) < 2:
                continue
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            k = (name, key)
            h = hits.setdefault(k, {"name": name, "event": key, "dates": set(), "ext_id": _ext_id(href),
                                    "href": href})
            h["dates"].add(d)
    out = []
    for h in hits.values():
        ds = sorted(h["dates"])
        out.append({"name": h["name"], "event": h["event"], "start": ds[0].isoformat(),
                    "end": ds[-1].isoformat(), "ext_id": h["ext_id"], "href": h["href"]})
    return out


def merge_months(rows: list[dict]) -> list[dict]:
    """월 경계를 넘는 일정(예: 10/30~11/2 수요예측)을 하나로 합친다."""
    acc: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = (r["name"], r["event"])
        if k in acc:
            acc[k]["start"] = min(acc[k]["start"], r["start"])
            acc[k]["end"] = max(acc[k]["end"], r["end"])
            acc[k]["ext_id"] = acc[k]["ext_id"] or r["ext_id"]
        else:
            acc[k] = dict(r)
    return list(acc.values())


def collect(today: date) -> tuple[list[dict], list[str]]:
    """반환: (일정 행 목록, 조회한 URL 목록). 한 달이라도 실패하면 예외를 그대로 올린다."""
    rows, urls = [], []
    for y, m in months_to_scan(today):
        url = month_url(y, m)
        html = fetch(url, encoding=None)
        for r in parse_calendar(html, y, m):
            r["url"] = url
            rows.append(r)
        urls.append(url)
    merged = merge_months(rows)
    for r in merged:
        r["url"] = month_url(*map(int, r["start"].split("-")[:2]))
    return merged, urls
