# Keep playwright version here aligned with requirements.txt
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Hole Lab root CA so Python and Chromium trust local HTTPS notebooks.
# certutil (libnss3-tools) adds the cert to the NSS database that Chromium reads.
COPY lab-hole-root.crt /usr/local/share/ca-certificates/lab-hole-root.crt
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends libnss3-tools && \
    rm -rf /var/lib/apt/lists/* && \
    update-ca-certificates && \
    mkdir -p /root/.pki/nssdb && \
    certutil -N -d /root/.pki/nssdb --empty-password && \
    certutil -d /root/.pki/nssdb -A -t "CT,," -n "LabHoleCA" \
        -i /usr/local/share/ca-certificates/lab-hole-root.crt

COPY app/ ./app/
COPY scripts/ ./scripts/

# config.yaml is baked into the image. The dev box's Docker daemon is a
# Docker-in-Docker container (DOCKER_HOST=tcp://claude-docker:2376) whose
# filesystem doesn't contain this workspace, so host bind mounts can't reach
# config.yaml — but the build context is streamed to the daemon and works.
# Trade-off: editing config.yaml requires `docker compose build` again.
COPY config.yaml ./config.yaml
ENV CONFIG_PATH=/app/config.yaml

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
