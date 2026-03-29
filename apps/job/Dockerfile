FROM mirror.gcr.io/library/python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/Josh-Archer/cleanarr"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY cleanarr ./cleanarr
COPY apps/job ./apps/job

RUN pip install --no-cache-dir .

RUN mkdir -p /logs

CMD ["python", "apps/job/main.py"]
