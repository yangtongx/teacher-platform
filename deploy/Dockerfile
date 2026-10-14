FROM python:3.12-slim-bookworm

RUN groupadd --system --gid 10001 teacher \
    && useradd --system --uid 10001 --gid 10001 --home-dir /home/teacher --create-home --shell /usr/sbin/nologin teacher

WORKDIR /srv/teacher-platform
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
COPY workspaces ./workspaces

USER 10001:10001
EXPOSE 18900
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18900/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18900"]
