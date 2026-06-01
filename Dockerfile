FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Data is stored in a volume so it persists across restarts
VOLUME ["/app/data"]

CMD ["python", "bot.py"]
