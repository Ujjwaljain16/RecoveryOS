#!/usr/bin/env bash
# RecoveryOS — demo bootstrap.
#
# Wraps the exact manual sequence documented in README.md's "Run the Demo"
# section (docker compose up, health check, merchant/API-key seeding) into
# one command. Doesn't touch application code, doesn't touch benchmark
# behavior — pure convenience over commands that already exist and are
# independently documented in the README.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "No .env found -- copying .env.example. Fill in the placeholders (passwords, pepper),"
  echo "then re-run this script."
  cp .env.example .env
  exit 1
fi

echo "== Starting the stack (postgres, redis, migrate, api, workers, prometheus, grafana) =="
docker compose up -d --build

echo "== Waiting for the API to become healthy =="
for _ in $(seq 1 30); do
  if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "API is healthy."
    break
  fi
  sleep 2
done

if ! curl -sf http://localhost:8000/health > /dev/null 2>&1; then
  echo "API did not become healthy in time -- check 'docker compose logs api'."
  exit 1
fi

echo "== Seeding a demo merchant + API key =="
OUTPUT=$(docker compose exec -T api python -c "
from apps.api.dependencies.auth import generate_api_key, hash_api_key
key = generate_api_key()
print(key)
print(hash_api_key(key))
")
API_KEY=$(echo "$OUTPUT" | sed -n '1p')
API_KEY_HASH=$(echo "$OUTPUT" | sed -n '2p')

docker compose exec -T postgres psql -U recoveryos -d recoveryos -c "
INSERT INTO merchants (merchant_id, name, api_key_hash)
VALUES (gen_random_uuid(), 'demo-merchant', '${API_KEY_HASH}');
" > /dev/null

cat <<EOF

== Demo ready ==

API key (save this, it is shown once): ${API_KEY}

Dashboard:
  cd apps/dashboard && npm install && npm run dev
  then open http://localhost:3000

Trigger the hero scenario (fails once, replans, succeeds):
  curl -X POST http://localhost:8000/v1/simulate/scenario \\
    -H "X-API-Key: ${API_KEY}" -H "Content-Type: application/json" \\
    -d '{"scenario": "recover_via_replan"}'

Note: recover_via_replan / safety_escalation additionally need
AI_RECOMMENDATION_FUSION_ENABLED=true. If the stack above wasn't started with
that, bring it up again with the extra override before triggering a scenario:
  AI_RECOMMENDATION_FUSION_ENABLED=true docker compose \\
    -f docker-compose.yml -f docker-compose.override.ai_fusion.yml up -d --build

Reset:
  docker compose down -v && ./demo.sh
EOF
