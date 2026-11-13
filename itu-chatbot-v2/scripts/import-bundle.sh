#!/usr/bin/env bash
# Restore a bundle made by export-bundle.sh on the target server.
# Usage: scripts/import-bundle.sh /path/to/bundle-YYYY-MM-DD
set -euo pipefail
B=${1:?bundle directory}
( cd "$B" && sha256sum -c SHA256SUMS )

docker load < "$B/images.tar.gz"
for v in itu-chatbot-ollama itu-chatbot-open-webui itu-chatbot-ingest-state; do
  if docker volume inspect "$v" >/dev/null 2>&1; then
    echo "volume $v already exists — refusing to overwrite. Remove it deliberately first." >&2; exit 1
  fi
  docker volume create "$v" >/dev/null
  docker run --rm -v "$v":/v -v "$(realpath "$B")":/in:ro alpine:3 tar -C /v -xzf "/in/$v.tar.gz"
  echo ">> restored $v"
done
echo "Now: cp $B/docker-compose.yml + your .env into the deploy dir, then  docker compose up -d"
