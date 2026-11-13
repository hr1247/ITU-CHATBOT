#!/usr/bin/env bash
# Package the whole deployment for an air-gapped server:
#   images (ollama, open-webui, ingest) + volumes (models, Open WebUI data incl. vectors, ingest state)
# Stops the stack briefly so the SQLite/vector files are consistent.
# Usage: scripts/export-bundle.sh [output-dir]        (default ./bundle-YYYY-MM-DD)
#        INCLUDE_USERS=0 is not supported: the open-webui volume holds accounts and chats too —
#        treat the bundle as sensitive and move it only through an approved channel.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:-bundle-$(date +%F)}
mkdir -p "$OUT"

docker compose --profile ingest build ingest
docker pull alpine:3 >/dev/null   # used by import-bundle.sh to restore volumes
IMAGES="$(docker compose --profile ingest config --images | sort -u) alpine:3"
echo ">> saving images:"; echo "$IMAGES"
docker save $IMAGES | gzip > "$OUT/images.tar.gz"

docker compose stop
for v in itu-chatbot-ollama itu-chatbot-open-webui itu-chatbot-ingest-state; do
  echo ">> saving volume $v"
  docker run --rm -v "$v":/v:ro -v "$PWD/$OUT":/out alpine:3 \
    tar -C /v -czf "/out/$v.tar.gz" .
done
docker compose start

cp docker-compose.yml .env.example "$OUT/"
cp -r ingest deploy scripts "$OUT/"
( cd "$OUT" && sha256sum *.tar.gz > SHA256SUMS )
echo "Bundle written to $OUT (copy .env separately; it is not included)."
