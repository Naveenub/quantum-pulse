FROM python:3.12-slim

LABEL maintainer="QUANTUM-PULSE"
LABEL description="Extreme-density data vault engine for LLM training sets"

# System deps (OpenSSL for AES-NI, build tools for zstandard)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create logs directory
RUN mkdir -p /app/logs

# Non-root user
RUN useradd -m -u 1000 qpulse && chown -R qpulse:qpulse /app
USER qpulse

EXPOSE 8747

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8747/health || exit 1

CMD ["python", "main.py"]
