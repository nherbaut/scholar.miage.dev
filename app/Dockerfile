# Production Dockerfile for MIAGE Scholar (Flask + Socket.IO)
# Serves via Gunicorn with gevent-websocket worker.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps (minimal). If builds fail on your platform, consider adding build-essential.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy app sources
COPY app ./app
COPY uwsgi.ini ./uwsgi.ini

# Environment
ENV FLASK_DEBUG=0 \
    PORT=8000

EXPOSE 8000

# Start with Gunicorn + gevent-websocket worker
CMD ["gunicorn", "-k", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", "-w", "1", "-b", "0.0.0.0:8000", "app.main:app"]

