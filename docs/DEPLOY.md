# IPO Radar 서버 배포 안내

배포하면 부서원이 웹 주소로 접속해 로그인 후 언제든 볼 수 있습니다. 서버가 매주 월요일 08:00에 자동으로 수집하고, 관리자는 화면의 '지금 갱신'으로 바로 수집할 수 있습니다.

> **먼저 써 보고 싶다면**: `docs/QUICKSTART_RENDER.md`를 따라 하면 Render 무료 플랜에 10분 만에 인터넷 주소를 만들 수 있습니다(검증용).

## 1. 어디에 올릴지 정하기

| 방식 | 언제 | 접속 주소 예 |
|---|---|---|
| **A. 사내 서버(권장)** | 회사 내부망에서만 쓰는 경우 | `http://10.x.x.x:8080` |
| **B. 클라우드 서버** | 사외에서도 접속해야 하는 경우 | `https://ipo.회사도메인` |

회사 로고와 지점명이 들어간 화면입니다. 서버를 두기 전에 **사내 IT·정보보호 담당 부서의 승인**을 받으세요. 특히 사외 접속(B)은 회사 보안 정책상 허용되지 않을 수 있습니다.

필요한 서버 사양: 리눅스, CPU 1개, 메모리 1GB, 디스크 5GB면 충분합니다.

서버가 바깥으로 접속할 수 있어야 하는 주소는 다음과 같습니다.
- `opendart.fss.or.kr`
- `openapi.naver.com`
- `www.googleapis.com`
- `www.ipostock.co.kr`
- `data-dbg.krx.co.kr`

## 2. Docker로 설치하기 (A·B 공통)

1. 서버에 Docker를 설치하고, 코드 압축 파일을 올려 풉니다.
2. 접속 계정의 비밀번호 해시를 만듭니다. 사람마다 한 번씩 실행합니다.
   ```bash
   python3 -m ipo_radar hash-password 홍길동
   ```
   출력된 `홍길동:pbkdf2_sha256$...:viewer` 줄을 복사합니다. 관리자는 끝의 `:viewer`를 `:admin`으로 바꿉니다.
3. `.env.example`을 `.env`로 복사한 뒤 아래 값을 채웁니다.
   - `IPO_RADAR_USERS`: 2단계에서 만든 줄들을 `;`로 이어 붙입니다.
   - `DART_API_KEY`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, `YOUTUBE_API_KEY`
   - `DOMAIN`: B 방식만
4. 실행합니다.
   - **B. 클라우드**: 도메인의 DNS를 서버 IP로 연결한 뒤 실행합니다. HTTPS 인증서는 자동으로 발급·갱신됩니다.
     ```bash
     docker compose up -d --build
     ```
   - **A. 사내**: `docker-compose.yml`에서 `caddy` 서비스를 지우고, `app`의 `ports` 줄 주석을 푼 뒤 같은 명령을 실행합니다.
5. 브라우저로 접속해 로그인합니다. 첫 실행 때 자동으로 수집하므로 1분쯤 뒤 화면이 나옵니다.

## 3. Docker 없이 설치하기 (사내 리눅스 서버)

1. 코드를 `/opt/ipo-radar`에 둡니다(Python 3.11 이상 필요, 추가 패키지 없음).
2. 설정 파일 `/etc/ipo-radar.env`를 만들고 2장 3단계의 값을 넣습니다. `chmod 600`으로 권한을 막아 둡니다.
3. `deploy/ipo-radar-web.service`를 `/etc/systemd/system/`에 복사한 뒤 실행합니다.
   ```bash
   sudo systemctl enable --now ipo-radar-web
   ```
4. HTTPS는 사내 리버스 프록시(nginx 등) 뒤에 두세요.

## 4. 운영

| 할 일 | 방법 |
|---|---|
| 상태 확인 | `https://주소/healthz`가 `ok`이면 정상 |
| 로그 보기 | `docker compose logs -f app` |
| 즉시 수집 | 관리자 계정으로 로그인 → 상단 '지금 갱신' |
| 계정 추가·삭제 | `.env`의 `IPO_RADAR_USERS`를 고친 뒤 `docker compose up -d` (퇴사·전출자는 바로 삭제) |
| 업데이트 | 새 코드를 올리고 `docker compose up -d --build` (데이터는 볼륨에 남음) |
| 백업 | `docker compose cp app:/var/lib/ipo-radar/ipo_radar.sqlite3 ./backup-$(date +%F).sqlite3` (주 1회 권장) |

## 5. 보안 기본값
- 로그인 없이는 `/healthz` 외에 아무것도 볼 수 없습니다.
- 로그인을 10회 실패하면 해당 IP를 15분간 차단합니다.
- 비밀번호는 원문 대신 PBKDF2 해시(26만 회 반복)로만 저장합니다.
- 제공 파일은 허용 목록(대시보드, data.json, 캘린더, CSV)만 내보냅니다. DB와 설정 파일은 웹에서 접근할 수 없습니다.
- 다른 사이트에 끼워 넣기(iframe)와 외부 스크립트 실행을 막는 보안 헤더(CSP, X-Frame-Options 등)를 보냅니다.
- 수집 실행은 관리자 계정만 가능합니다. 같은 수집이 동시에 두 번 돌지 않습니다.
- 기본 인증은 반드시 HTTPS 뒤에서 쓰세요. B 방식은 Caddy가 자동 처리하고, A 방식은 사내망 안에서만 쓰거나 사내 프록시로 HTTPS를 적용하세요.
