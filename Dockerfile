FROM python:3.11-slim

WORKDIR /app

# Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода
COPY . .

# Переменные окружения должны передаваться при запуске
# BOT_TOKEN, ADMIN_ID, PAYMENT_URL

CMD ["python", "bot.py"]
