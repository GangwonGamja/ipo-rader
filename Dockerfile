# IPO Radar 웹 서버 이미지 (외부 패키지 없음, 표준 라이브러리만 사용)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Seoul \
    IPO_RADAR_DB=/var/lib/ipo-radar/ipo_radar.sqlite3 \
    IPO_RADAR_OUTPUT_DIR=/var/lib/ipo-radar/www \
    IPO_RADAR_PORT=8080
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY ipo_radar ./ipo_radar
COPY dashboard/template.html dashboard/brand_logo.png ./dashboard/
COPY data/seed ./data/seed
COPY data/manual_inputs ./data/manual_inputs
COPY data/brand.json ./data/brand.json
RUN useradd --system --uid 10001 ipo && mkdir -p /var/lib/ipo-radar/www && chown -R ipo /var/lib/ipo-radar
USER ipo
VOLUME ["/var/lib/ipo-radar"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"
CMD ["python", "-m", "ipo_radar", "serve-prod"]
