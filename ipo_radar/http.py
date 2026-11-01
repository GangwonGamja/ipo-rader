"""HTTP 수집 공통 계층: 재시도, robots.txt 준수, 테스트용 교체 가능 전송 계층.

접근 제한(401/403/429, robots 금지)은 우회하지 않고 즉시 'blocked'로 보고한다.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from typing import Callable

from . import config


class FetchError(Exception):
    def __init__(self, msg: str, *, blocked: bool = False, status: int | None = None):
        super().__init__(msg)
        self.blocked = blocked
        self.status = status


# 테스트·오프라인 재현용: (url, headers) -> (status, bytes)
Transport = Callable[[str, dict], tuple[int, bytes]]


def _urllib_transport(url: str, headers: dict, data: bytes | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b""


_transport: Transport = _urllib_transport
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_dead_hosts: dict[str, str] = {}  # 이번 실행에서 재시도까지 모두 실패한 호스트 → 즉시 실패(회로 차단)
_sleep = time.sleep


def reset_run_state():
    _robots_cache.clear()
    _dead_hosts.clear()


def set_transport(t: Transport | None, sleep: Callable[[float], None] | None = None):
    global _transport, _sleep
    _transport = t or _urllib_transport
    _sleep = sleep or time.sleep
    reset_run_state()


def robots_allowed(url: str, check: bool = True) -> bool:
    if not check:
        return True
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots_cache:
        try:
            status, body = _transport(base + "/robots.txt", {"User-Agent": config.USER_AGENT})
            if status == 200:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(body.decode("utf-8", "replace").splitlines())
                _robots_cache[base] = rp
            else:
                _robots_cache[base] = None  # robots.txt 없음 → 일반 규칙
        except Exception:
            _robots_cache[base] = None
    rp = _robots_cache[base]
    return True if rp is None else rp.can_fetch(config.USER_AGENT, url)


def fetch(url: str, *, headers: dict | None = None, check_robots: bool = True,
          attempts: int | None = None, encoding: str | None = None, data: bytes | None = None, raw: bool = False):
    """텍스트를 가져온다. 일시 오류는 지수 백오프로 재시도, 차단은 즉시 실패."""
    host = urllib.parse.urlparse(url).netloc
    if host in _dead_hosts:
        raise FetchError(f"이번 실행에서 이미 실패한 호스트({_dead_hosts[host]}): {host}")
    if not robots_allowed(url, check_robots):
        raise FetchError(f"robots.txt가 수집을 허용하지 않음: {url}", blocked=True)
    h = {"User-Agent": config.USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
    h.update(headers or {})
    attempts = attempts or config.RETRY_ATTEMPTS
    last = None
    for i in range(attempts):
        try:
            status, body = _transport(url, h) if data is None else _transport(url, h, data)
        except Exception as e:  # 네트워크 오류
            if "403" in str(e) or "407" in str(e):  # 프록시·방화벽 정책 거부 → 재시도하지 않음
                _dead_hosts[host] = "정책 거부"
                raise FetchError(f"접근 거부(네트워크 정책): {host} — {e}", blocked=True)
            last = FetchError(f"{type(e).__name__}: {e}")
        else:
            if status == 200:
                if raw:
                    return body
                if encoding:
                    return body.decode(encoding, "replace")
                for enc in ("utf-8", "cp949"):
                    try:
                        return body.decode(enc)
                    except UnicodeDecodeError:
                        continue
                return body.decode("utf-8", "replace")
            if status in (401, 403, 407, 451):
                _dead_hosts[host] = f"HTTP {status}"
                raise FetchError(f"접근 거부 HTTP {status}: {url}", blocked=True, status=status)
            last = FetchError(f"HTTP {status}: {url}", status=status)
            if status == 404:
                raise last
        if i < attempts - 1:
            _sleep(config.RETRY_BACKOFF_SEC * (2 ** i))
    if last is not None and last.status is None:  # 네트워크 수준 실패만 회로 차단
        _dead_hosts[host] = str(last)[:80]
    raise last  # type: ignore[misc]


def fetch_json(url: str, **kw):
    return json.loads(fetch(url, **kw))


def post_json(url: str, payload: dict, **kw):
    h = dict(kw.pop("headers", {}) or {})
    h["Content-Type"] = "application/json"
    return json.loads(fetch(url, headers=h, data=json.dumps(payload).encode("utf-8"), **kw))
