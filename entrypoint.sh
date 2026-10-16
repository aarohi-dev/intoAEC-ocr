#!/bin/bash
set -e

echo "=========================================================="
echo " Starting intoAEC-ocr • Construction BOQ Intelligence"
echo "=========================================================="

# 1. Start Ollama daemon in background
export OLLAMA_HOST=0.0.0.0:11434
echo "[1/3] Launching Ollama server (0.0.0.0:11434)..."
ollama serve > /tmp/ollama.log 2>&1 &
OLLAMA_PID=$!

# Trap signals for graceful shutdown
cleanup() {
    echo "Stopping services..."
    kill -TERM "$OLLAMA_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

# 2. Wait for Ollama service to become healthy
echo "[2/3] Waiting for Ollama engine to initialize..."
MAX_RETRIES=30
RETRY_COUNT=0
until curl -s http://127.0.0.1:11434/api/tags > /dev/null 2>&1; do
    sleep 1
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -ge $MAX_RETRIES ]; then
        echo "Warning: Ollama took longer than expected to initialize. Proceeding anyway."
        break
    fi
done

# 3. Ensure qwen2.5:1.5b model is available
TARGET_MODEL=${OLLAMA_MODEL:-"qwen2.5:1.5b"}
echo "Checking model: $TARGET_MODEL..."

if ollama list 2>/dev/null | grep -q "$TARGET_MODEL"; then
    echo "✓ Model '$TARGET_MODEL' is ready in local cache."
else
    echo "Pulling '$TARGET_MODEL' into container cache..."
    ollama pull "$TARGET_MODEL" || echo "Notice: Model pull failed or offline; continuing with native heuristic fallback."
fi

# 4. Start FastAPI Application
echo "[3/3] Starting intoAEC-ocr web dashboard on http://0.0.0.0:8000 ..."
exec uvicorn app:app --host 0.0.0.0 --port 8000
