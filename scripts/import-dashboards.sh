#!/usr/bin/env bash
# Importa todos os dashboards JSON para o Grafana via API.
# Usar após make up em setup fresh (file provisioning desabilitado no Grafana 13).
set -euo pipefail

GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_USER="${GRAFANA_USER:-admin}"
GRAFANA_PASS="${GRAFANA_PASS:-admin}"
DASHBOARDS_DIR="$(dirname "$0")/../config/grafana/dashboards"
FOLDER_UID="ffn54ww76za4gf"
FOLDER_TITLE="Lab"

ensure_folder() {
  curl -sf -u "${GRAFANA_USER}:${GRAFANA_PASS}" \
    -X POST "${GRAFANA_URL}/api/folders" \
    -H "Content-Type: application/json" \
    -d "{\"uid\":\"${FOLDER_UID}\",\"title\":\"${FOLDER_TITLE}\"}" \
    > /dev/null 2>&1 || true
}

import_dashboard() {
  local file="$1"
  local name
  name=$(basename "$file")

  local payload
  payload=$(python3 -c "
import json, sys
with open('${file}') as f:
    d = json.load(f)
d.pop('id', None)
d.pop('version', None)
print(json.dumps({'dashboard': d, 'folderUid': '${FOLDER_UID}', 'overwrite': True}))
")

  local result
  result=$(curl -sf -u "${GRAFANA_USER}:${GRAFANA_PASS}" \
    -X POST "${GRAFANA_URL}/api/dashboards/db" \
    -H "Content-Type: application/json" \
    -d "$payload")

  local status
  status=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "error")

  if [ "$status" = "success" ]; then
    echo "  [OK] $name"
  else
    echo "  [ERRO] $name — $result"
    return 1
  fi
}

echo "Importando dashboards para o Grafana (${GRAFANA_URL})..."
ensure_folder

for json_file in "${DASHBOARDS_DIR}"/*.json; do
  import_dashboard "$json_file"
done

echo "Concluído."
