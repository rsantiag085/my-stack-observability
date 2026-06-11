#!/usr/bin/env bash
set -euo pipefail

cd "$(cd "$(dirname "$0")/.." && pwd)"

echo "Parando containers e removendo volumes..."
docker compose down -v --remove-orphans

echo "Removendo certificados gerados..."
rm -f config/nginx/certs/lab.crt config/nginx/certs/lab.key

echo "Gerando novos certificados..."
./scripts/gen-certs.sh

echo "Subindo o lab..."
docker compose up -d --build

echo "Lab recriado com sucesso."
