FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

WORKDIR /app

# Сначала зависимости — этот слой кэшируется и не пересобирается,
# пока не меняется requirements.txt
COPY requirements.txt .
RUN pip install -r requirements.txt

# Код бота (что НЕ попадает в образ — см. .dockerignore)
COPY . .

# База SQLite живёт в /app/data — это volume, он переживает пересборки
RUN mkdir -p /app/data

# Секреты (BOT_TOKEN и т.д.) передаются через env_file в docker-compose.yml,
# в образ они не попадают
CMD ["python", "bot.py"]
