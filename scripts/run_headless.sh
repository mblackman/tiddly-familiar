#!/usr/bin/env bash
# Run one of the Playwright dev scripts (verify_plugin_*.py, shot_*.py)
# against the compose network. The gateway image no longer ships a browser,
# so these run in a throwaway Playwright container that joins the same
# network as tw-dev and the gateway.
set -euo pipefail

script="${1:?usage: scripts/run_headless.sh scripts/<script>.py}"
image="mcr.microsoft.com/playwright/python:v1.49.0-noble"
network="${HEADLESS_NETWORK:-tiddly-familiar_default}"
name="tpwa-headless-$$"

docker run -d --name "$name" --network "$network" "$image" sleep infinity >/dev/null
trap 'docker rm -f "$name" >/dev/null' EXIT

# The image ships the browsers (/ms-playwright) but not the pip package.
docker exec "$name" pip install --quiet --break-system-packages playwright==1.49.0

docker cp "$script" "$name:/tmp/job.py"
docker exec "$name" python /tmp/job.py

# Bring back any screenshots the script left in /tmp.
mkdir -p build
for f in $(docker exec "$name" sh -c 'ls /tmp/*.png 2>/dev/null || true'); do
    docker cp "$name:$f" "build/$(basename "$f")"
    echo "copied $(basename "$f") -> build/"
done
