FROM python:3.14-slim

LABEL org.opencontainers.image.source="https://github.com/example/cleanarr-runtime"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY cleanarr_runtime ./cleanarr_runtime
COPY apps/job ./apps/job

RUN pip install --no-cache-dir .

RUN mkdir -p /logs

CMD ["python", "apps/job/main.py"]
