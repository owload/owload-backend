FROM python:3.12-slim

LABEL org.opencontainers.image.title="owload-backend" \
      org.opencontainers.image.description="The API of Owload, an encrypted virtual drive" \
      org.opencontainers.image.source="https://github.com/owload/owload-back"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# The exact versions the image is built with (requirements.lock, made from requirements.txt for this Python; see the README).
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Only what runs: nothing else of the working directory is copied (and .dockerignore keeps .env and the like out anyway).
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# The API does not need to be root.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin owload
USER owload

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

# The database is brought up to date, then the API starts. The workers use this image with another command (see the README).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
