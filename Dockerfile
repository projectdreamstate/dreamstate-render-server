FROM python:3.11-slim

# Install FFmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# 1800s timeout = 30 min max per render job
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "1800", "--workers", "2", "app:app"]
