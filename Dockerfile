FROM python:3.12-slim

# System deps used by cryptg / telethon native extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first to leverage layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY src/ ./src/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Sessions live on a persistent volume
ENV SESSION_DIR=/data/sessions
RUN mkdir -p /data/sessions
VOLUME ["/data/sessions"]

# Make python output unbuffered so docker logs stream live
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--help"]
