FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Kyiv \
    DATABASE_PATH=/app/data/bot.sqlite3

WORKDIR /app
RUN groupadd --gid 10001 bot && useradd --uid 10001 --gid bot --no-create-home bot \
    && mkdir /app/data && chown bot:bot /app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=bot:bot *.py ./
USER bot
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "healthcheck.py"]
CMD ["python", "main.py"]
