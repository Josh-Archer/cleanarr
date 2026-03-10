FROM python:3.14-slim

LABEL org.opencontainers.image.source="https://github.com/example/cleanarr-runtime"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md cleanarr.py cleanarr_webhook.py ./
COPY cleanarr_runtime ./cleanarr_runtime
COPY apps/webhook ./apps/webhook

RUN pip install --no-cache-dir .

RUN mkdir -p /logs

CMD ["python", "apps/webhook/main.py"]
