FROM python:3.13-slim

WORKDIR /app

# Копируем requirements и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY bot/ ./bot/

# Копируем файлы (изображения и т.д.)
COPY files/ ./files/

# Устанавливаем рабочую директорию для бота
WORKDIR /app/bot

# Запускаем бота
CMD ["python", "main.py"]

