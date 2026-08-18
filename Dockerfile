FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATA_DIR=/data

WORKDIR /app

COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
RUN mkdir -p /data && chown -R nobody:nogroup /data

USER nobody
EXPOSE 7272

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7272/health', timeout=2)" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:7272", "--workers", "2", "--threads", "4", "--timeout", "60", "app:app"]
