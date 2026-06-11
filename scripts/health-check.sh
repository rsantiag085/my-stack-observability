#!/usr/bin/env bash
set -euo pipefail

PASS=0
FAIL=0

check() {
  local name="$1"
  local url="$2"
  if curl -sf --max-time 5 "$url" > /dev/null 2>&1; then
    echo "  OK  $name"
    PASS=$((PASS + 1))
  else
    echo " FAIL $name  ($url)"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== Health Check — Observability Lab ==="
echo ""

check "Prometheus"      "http://localhost:9090/-/healthy"
check "Grafana"         "http://localhost:3000/api/health"
check "Loki"            "http://localhost:3100/ready"
check "Tempo"           "http://localhost:3200/ready"
check "OTEL Collector"  "http://localhost:13133/"
check "Promtail"        "http://localhost:9080/ready"

echo ""
echo "Resultado: ${PASS} OK | ${FAIL} FALHA(S)"
[ "$FAIL" -eq 0 ]
