FROM python:3.12-slim

WORKDIR /app

# The exact versions the image is built with (requirements.lock, made from requirements.txt for this Python; see the README).
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
