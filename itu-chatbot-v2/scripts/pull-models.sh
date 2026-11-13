#!/usr/bin/env bash
# Give the ollama container internet access just long enough to pull the models, then take it away.
# Usage: scripts/pull-models.sh            (reads LLM_MODEL and EMBEDDING_MODEL from .env)
#        scripts/pull-models.sh llama3.2:3b (extra models)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

CID=$(docker compose ps -q ollama)
[ -n "$CID" ] || { echo "ollama is not running: docker compose up -d first" >&2; exit 1; }
NET=itu-chatbot-pull

docker network create "$NET" >/dev/null 2>&1 || true
docker network connect "$NET" "$CID"
trap 'docker network disconnect "$NET" "$CID" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true' EXIT

for m in "${LLM_MODEL:-qwen3.5:4b}" "${EMBEDDING_MODEL:-qwen3-embedding:0.6b}" "$@"; do
  echo ">> pulling $m"
  docker compose exec -T ollama ollama pull "$m"
done
docker compose exec -T ollama ollama list
echo "Done. ollama is back on the internal network only."
