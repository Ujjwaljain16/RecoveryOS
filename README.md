# RecoveryOS

AI-driven revenue recovery control plane for Razorpay's hackathon Track 03.
Ensures separation between the LLM diagnosis layer and deterministic recovery policies
(see `docs/TRD.md` §1.1) — the LLM explains failures, a deterministic propensity/EVI/policy
engine decides what to do about them.

## Running it

### Prerequisites

- Docker + Docker Compose v2 (`docker compose`, not the standalone `docker-compose` v1)
- Node.js 18+ and npm (only needed if you're running the dashboard outside Docker)

### 1. Configure environment

```bash
cp .env.example .env
```

Fill in the `<CHANGE_ME>` placeholders — see `.env.example`'s own comments for exactly what
each one is for and how to generate it (mostly `python -c "import secrets; print(secrets.token_urlsafe(24))"`).
At minimum you need:

- `RECOVERYOS_APP_ROLE_PASSWORD` / `RECOVERYOS_DIAGNOSER_ROLE_PASSWORD` / `RECOVERYOS_INFERENCE_ROLE_PASSWORD`
  — must match the passwords baked into the `DATABASE_URL*` connection strings right below them
- `GRAFANA_ADMIN_PASSWORD`
- `API_KEY_PEPPER`

Everything else (AI provider keys, Razorpay test-mode credentials, webhook secret) is optional —
the system runs on deterministic fallbacks/the simulator provider without them.

### 2. Start the stack

```bash
docker compose up -d
```

This brings up Postgres, Redis, a one-shot `migrate` service (runs Alembic migrations —
`docker compose` waits for it to complete successfully before starting anything that
depends on the schema), the API, all four background workers (`event_processor`,
`pipeline_orchestrator`, `execution_worker`, `retry_scheduler`), and Prometheus + Grafana.

Check everything is healthy:

```bash
docker compose ps
curl http://localhost:8000/health
```

### 3. Seed a merchant + API key

Every API route except `/health` and `/webhooks/razorpay` requires a real `X-API-Key` header,
verified against a hash stored in `merchants.api_key_hash` (`apps/api/dependencies/auth.py`).
There's no provisioning CLI yet — seed one directly:

```bash
# Generate a real key + its hash (run inside the api container so it picks up
# the same API_KEY_PEPPER your .env configured):
docker compose exec api python -c "
from apps.api.dependencies.auth import generate_api_key, hash_api_key
key = generate_api_key()
print('API key (save this, it is shown once):', key)
print('hash:', hash_api_key(key))
"

# Insert the merchant with that hash (replace <HASH> with the value printed above):
docker compose exec postgres psql -U recoveryos -d recoveryos -c "
INSERT INTO merchants (merchant_id, name, api_key_hash)
VALUES (gen_random_uuid(), 'demo-merchant', '<HASH>');
"
```

### 4. Reach it

| What | Where |
|---|---|
| API | `http://localhost:8000` (send `X-API-Key: <your key>` on every merchant-scoped route) |
| API docs (OpenAPI) | `http://localhost:8000/docs` |
| Dashboard | `cd apps/dashboard && npm install && npm run dev`, then `http://localhost:3000` |
| Grafana | `http://localhost:3001` (`admin` / your `GRAFANA_ADMIN_PASSWORD`) |
| Prometheus | `http://localhost:9090` |

Send a real payment-failure event to see the pipeline run end to end:

```bash
curl -X POST http://localhost:8000/v1/events \
  -H "X-API-Key: <your key>" -H "Content-Type: application/json" \
  -d '{"payment_id":"<uuid>","merchant_id":"<your merchant_id>","customer_id":"<uuid>",
       "amount_paise":50000,"method":"upi","bank":"HDFC","event_type":"PAYMENT_FAILED",
       "failure_code":"TIMEOUT"}'
```

(`payment_id`/`customer_id` must already exist as real rows — see `tests/integration/conftest.py`
for the minimal `payments`/`customers` insert shape used throughout the test suite.)

### Running tests

```bash
pip install -r requirements.txt   # or however your local env is set up
pytest tests/unit tests/integration
```

Integration tests spin up real Postgres/Redis via `testcontainers` — no separate setup needed,
just a running Docker daemon.

## Demo mode

Setting `ENV=demo` (the `.env.example` default) enables `POST /v1/simulate/degrade`
(PRD §38), which injects a real degradation into the live anomaly-detection pipeline for a
live demo — not a canned animation.
