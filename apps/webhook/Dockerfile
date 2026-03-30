FROM mirror.gcr.io/library/python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/Josh-Archer/cleanarr"

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.0 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY cleanarr ./cleanarr
COPY apps/webhook ./apps/webhook

RUN pip install --no-cache-dir .

RUN mkdir -p /logs

CMD ["python", "apps/webhook/main.py"]

