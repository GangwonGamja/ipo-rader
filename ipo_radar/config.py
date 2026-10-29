"""IPO Radar 설정. 운영 값은 환경변수로 덮어쓴다."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("IPO_RADAR_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("IPO_RADAR_DB", DATA_DIR / "ipo_radar.sqlite3"))
DASHBOARD_DIR = ROOT / "dashboard"          # 템플릿·로고(코드와 함께 배포)
OUTPUT_DIR = Path(os.environ.get("IPO_RADAR_OUTPUT_DIR", DASHBOARD_DIR))  # 생성된 대시보드(서버는 볼륨 경로 사용)
SEED_DIR = DATA_DIR / "seed"
MANUAL_DIR = DATA_DIR / "manual_inputs"

# 조회 범위: 실행일이 속한 달부터 향후 N일이 걸치는 모든 달
LOOKAHEAD_DAYS = 90

# 스케줄: 매주 월요일 08:00 KST
SCHEDULE_WEEKDAY = 0  # Monday
SCHEDULE_HOUR = 8

# 데이터가 이 일수보다 오래되면 '오래된 데이터'로 표시
STALE_DAYS = 8

# 수집 재시도
RETRY_ATTEMPTS = int(os.environ.get("IPO_RADAR_RETRY", "3"))
RETRY_BACKOFF_SEC = float(os.environ.get("IPO_RADAR_BACKOFF", "2"))
HTTP_TIMEOUT = 20
USER_AGENT = "IPO-Radar/0.1 (+research dashboard; contact: set IPO_RADAR_CONTACT)"

# API 키 (없으면 해당 소스는 '설정 필요' 상태로 표시)
DART_API_KEY = os.environ.get("DART_API_KEY", "")
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
KRX_AUTH_KEY = os.environ.get("KRX_AUTH_KEY", "")

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
# 커뮤니티 게시판 직접 수집(디시인사이드 등)은 하지 않는다. 공식 API(네이버 검색·데이터랩, YouTube)만 사용.

# 기관 수요 강도 점수 가중치 (수요예측 결과)
DEMAND_WEIGHTS = {"competition": 0.40, "lockup": 0.30, "above_top": 0.20, "price_position": 0.10}

# 관심도 점수 초기 가중치 (합 = 1.0)
INTEREST_WEIGHTS = {
    "volume": 0.35,     # 언급량
    "growth": 0.30,     # 증가세
    "diversity": 0.20,  # 출처 다양성
    "engagement": 0.15, # 참여도
}
# 소표본 급증 보정: 증가율 계산 시 양쪽 기간에 더하는 사전 표본 수
GROWTH_PRIOR = 5
# 산출 가능한 구성요소 가중치 합이 이 값 미만이면 점수를 내지 않는다
MIN_COMPONENT_COVERAGE = 0.5
# 비교 집단 최소 크기 (미만이면 전체 후보와 비교하고 표시)
MIN_PEER_GROUP = 3

# 가치평가
MIN_VALID_PEERS = 3            # 배수별 유효 비교기업 최소 수
FORECAST_HAIRCUTS = [0.0, 0.2, 0.4]  # 회사 제시 미래실적 민감도

# 주관사 산정 근거 재검토: 확인 실적 기준 가치 대비 공모가 할인율(실효 할인율) 판정 구간
# 실효 할인율 ≥ 30% → 상대적 저평가 가능성 / 15~30% → 적정 범위 / < 15% → 상대적 고평가 가능성
FAIR_DISCOUNT_LOW = 0.15
FAIR_DISCOUNT_HIGH = 0.30
# 미래 추정 의존도가 이 값 이상이면 위험요인으로 표시
FORECAST_DEPENDENCY_ALERT = 0.4

# 웹 서버
SERVER_HOST = os.environ.get("IPO_RADAR_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("PORT") or os.environ.get("IPO_RADAR_PORT", "8080"))  # 클라우드(Render 등)는 PORT 지정
# 사용자: "이름:pbkdf2해시:역할;이름:해시:역할" (역할 admin|viewer). 해시는 `python -m ipo_radar hash-password`로 생성
SERVER_USERS = os.environ.get("IPO_RADAR_USERS", "")
SERVER_ALLOW_ANON = os.environ.get("IPO_RADAR_ALLOW_ANON", "0") == "1"  # 테스트용. 운영에서는 0
SERVER_SCHEDULER = os.environ.get("IPO_RADAR_SCHEDULER", "1") == "1"
SERVER_WITH_SAMPLE = os.environ.get("IPO_RADAR_WITH_SAMPLE", "0") == "1"
# 빠른 설정용 관리자 계정(클라우드 비밀 환경변수에 넣음). 시작할 때 해시로 바꿔 메모리에만 보관
SERVER_ADMIN_USER = os.environ.get("IPO_RADAR_ADMIN_USER", "")
SERVER_ADMIN_PASSWORD = os.environ.get("IPO_RADAR_ADMIN_PASSWORD", "")
# 0 이면 회사 로고·지점명 없는 일반 화면을 첫 화면으로 사용
SERVER_BRAND = os.environ.get("IPO_RADAR_BRAND", "1") == "1"
