# Data pipeline image: Python geo stack + osmium-tool.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends osmium-tool ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /repo
COPY pipeline/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV PYTHONPATH=/repo/pipeline \
    PYTHONDONTWRITEBYTECODE=1
