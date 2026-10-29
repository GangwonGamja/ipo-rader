# 10분 만에 인터넷 접속 주소 만들기 (Render 무료 플랜, 검증용)

끝나면 `https://ipo-radar-○○○○.onrender.com` 같은 주소가 생깁니다. PC나 휴대폰 어디서든 접속해 로그인하면 됩니다.
카드 등록 없이 무료로 쓸 수 있습니다.

## 준비물
- GitHub 계정(없으면 github.com에서 무료 가입)
- 이 코드 압축 파일(`ipo-radar-render.zip`)

## 1단계 — GitHub에 코드 올리기 (3분)
1. github.com에 로그인 → 오른쪽 위 **+** → **New repository**
2. 이름: `ipo-radar`, 공개 범위는 **Private**(비공개) 선택 → **Create repository**
3. 새 저장소 화면에서 **uploading an existing file** 클릭
4. 압축을 푼 `ipo-radar` 폴더 **안의 내용 전체**(Dockerfile, render.yaml, ipo_radar 폴더 등)를 끌어다 놓기 → **Commit changes**
   - `Dockerfile`과 `render.yaml`이 저장소 첫 화면에 바로 보여야 합니다.

## 2단계 — Render에 배포하기 (5분)
1. render.com → **Get Started** → **GitHub로 로그인**
2. 대시보드에서 **New +** → **Blueprint**
3. 저장소 목록에서 `ipo-radar` 선택(안 보이면 **Configure GitHub App**에서 이 저장소 접근 허용)
4. 환경변수 입력 화면에서 두 개만 채웁니다.
   - `IPO_RADAR_ADMIN_USER`: 관리자 아이디(예: `gundae`)
   - `IPO_RADAR_ADMIN_PASSWORD`: 10자 이상 비밀번호
   - 나머지 API 키는 비워 둬도 됩니다(4단계에서 추가).
5. **Apply** → 빌드 로그가 흐르고 5~8분 뒤 **Live** 표시가 뜹니다.

## 3단계 — 접속 (1분)
1. 서비스 화면 위쪽의 `https://ipo-radar-○○○○.onrender.com` 주소 클릭
2. 로그인 창에 2단계에서 정한 아이디·비밀번호 입력
3. 처음 1분 정도는 "대시보드를 준비 중입니다"가 나옵니다. 첫 수집이 끝나면 새로고침하세요.

## 4단계 — 실제 데이터 연결로 검증하기
Render 서버는 인터넷 접속이 열려 있습니다. 그래서 개발 환경에서 막혀 있던 IPOSTOCK·DART·네이버·유튜브 수집을 여기서 처음으로 실제로 시험하게 됩니다.

1. Render 서비스 → **Environment**에서 키를 추가하고 **Save Changes**(자동 재시작)
   - `DART_API_KEY`: opendart.fss.or.kr → 인증키 신청(무료, 즉시~1일)
   - `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`: developers.naver.com → 애플리케이션 등록 → 사용 API에 '검색', '데이터랩(검색어트렌드)' 추가
   - `YOUTUBE_API_KEY`: console.cloud.google.com → YouTube Data API v3 사용 설정 → 사용자 인증 정보 → API 키
2. 대시보드 → **데이터 관리** 탭에서 자료원별 '정상/실패'를 확인합니다.
3. 관리자 계정이면 상단 **지금 갱신**으로 바로 다시 수집할 수 있습니다.

**IPOSTOCK 확인**: '데이터 관리'에서 IPOSTOCK이 '정상'인데 새로 잡힌 일정이 없으면, 실제 페이지 구조가 예상과 다른 것입니다. Render의 **Logs** 화면 내용을 보내 주시면 파서를 맞춰 드리겠습니다.

## 알아 두실 점 (무료 플랜)
- 15분간 아무도 접속하지 않으면 서버가 잠듭니다. 다음 접속 때 깨어나는 데 약 1분 걸립니다.
- 디스크가 유지되지 않아 재시작할 때마다 처음부터 다시 수집합니다. 그래서 변경 이력과 상담 메모가 쌓이지 않습니다.
- 서버가 잠들어 있으면 월요일 08:00 자동 수집이 그 시각에 돌지 않습니다. 다음에 깨어날 때 놓친 수집을 바로 실행합니다.
- 계속 쓰려면 유료 플랜(Starter + Persistent Disk)이나 사내 서버(`docs/DEPLOY.md`)로 옮기세요.
- 회사 로고가 들어간 화면이 인터넷 주소로 열립니다. 로그인으로 보호되지만, 사내 보안 정책상 문제가 될 수 있습니다. 승인 전에는 환경변수 `IPO_RADAR_BRAND=0`으로 로고 없는 화면으로 검증하는 것을 권합니다.
- 부서원 계정을 추가하려면 `IPO_RADAR_USERS`에 `이름:해시:viewer`를 `;`로 이어 넣습니다. 해시는 `python -m ipo_radar hash-password 이름`으로 만듭니다.
