# Data pipeline image: Python geo stack, osmium-tool and roofer.
# Debian 13 (trixie): roofer's release binary needs glibc 2.38+.
FROM python:3.12-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends osmium-tool ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# roofer (GPL-3.0) builds LoD 2.2 roofs from lidar; it runs as a separate program.
ADD --checksum=sha256:85834b289467b0e583e2b9c59a725c97ef39dc03c0c0a12417a8a0666e1aa0a1 \
    https://github.com/3DBAG/roofer/releases/download/v1.0.0/roofer-linux-x86_64-v1.0.0.tar.gz /tmp/roofer.tgz
RUN mkdir -p /opt/roofer && tar xzf /tmp/roofer.tgz -C /opt/roofer && rm /tmp/roofer.tgz \
 && chmod +x /opt/roofer/bin/roofer  # the release archive ships it without the execute bit
# Its own GDAL/PROJ data is passed only to the roofer process (see roofs.py), so the
# Python libraries keep using theirs.
ENV PATH=/opt/roofer/bin:$PATH

WORKDIR /repo
COPY pipeline/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV PYTHONPATH=/repo/pipeline \
    PYTHONDONTWRITEBYTECODE=1
