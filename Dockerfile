# intoAEC-ocr: Complete All-in-One Container with Native PDF Engine & Ollama LLM (Qwen2.5)
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_MODELS=/root/.ollama/models

# 1. Install system utilities and Ollama
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    procps \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://ollama.com/install.sh | sh

WORKDIR /app

# 2. Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 3. Pre-bake the Qwen2.5 model into the image during build
RUN ollama serve & \
    PID=$! && \
    sleep 3 && \
    (ollama pull qwen2.5:1.5b || echo "Model download deferred to container startup") && \
    kill $PID 2>/dev/null || true

# 4. Copy application source and assets
COPY boqparser /app/boqparser
COPY app.py /app/app.py
COPY standard_schedule_of_rates.json /app/standard_schedule_of_rates.json
COPY test_documents /app/test_documents
COPY output.json /app/output.json
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

# Expose FastAPI Web Dashboard (8000) and Ollama LLM Server (11434)
EXPOSE 8000 11434

ENTRYPOINT ["/app/entrypoint.sh"]