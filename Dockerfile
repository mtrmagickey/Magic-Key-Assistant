FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
COPY LeisureLLM/requirements.txt LeisureLLM/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r LeisureLLM/requirements.txt

# Application
COPY . .

# Non-root user for least-privilege runtime
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data /app/LeisureLLM

EXPOSE 8000

ENV PYTHONUNBUFFERED=1
ENV DATABASE_PATH=/app/data/assistant.db

USER appuser
WORKDIR /app/LeisureLLM
CMD ["python", "leisureLLM.py"]
