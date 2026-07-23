FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# data dir
RUN mkdir -p /data /app/exports && \
    touch /app/predskazbot.sqlite3 /app/predskazbot_v2.sqlite3 || true

VOLUME ["/data", "/app/exports", "/app/AI_Knowledge_Base"]

CMD ["python", "bot.py"]
