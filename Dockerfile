FROM python:3.12-slim-bookworm

ARG TARGETARCH
ARG OUTLINE_VERSION=1.9.2

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl tar && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in amd64) arch=x86_64;; arm64) arch=arm64;; *) echo "Unsupported architecture: ${TARGETARCH}"; exit 1;; esac; \
    base="https://github.com/OutlineFoundation/tunnel-server/releases/download/v${OUTLINE_VERSION}"; \
    asset="outline-ss-server_${OUTLINE_VERSION}_linux_${arch}.tar.gz"; \
    curl -fsSL --retry 3 "${base}/checksums.txt" -o /tmp/checksums.txt; \
    curl -fsSL --retry 3 "${base}/${asset}" -o "/tmp/${asset}"; \
    grep -F "${asset}" /tmp/checksums.txt > /tmp/asset-checksum.txt; \
    (cd /tmp && sha256sum -c /tmp/asset-checksum.txt); \
    tar -xzf "/tmp/${asset}" -C /usr/local/bin outline-ss-server; \
    chmod 0755 /usr/local/bin/outline-ss-server; \
    rm -f /tmp/checksums.txt /tmp/asset-checksum.txt "/tmp/${asset}"

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app/ /app/app/
RUN mkdir -p /data /run/outline-wss && chown -R 10001:10001 /app /run/outline-wss
ENV PYTHONUNBUFFERED=1
EXPOSE 636 18081 18443
USER 10001:10001
CMD ["python", "-m", "app.supervisor"]
