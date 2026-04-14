# Running LAWA Scouts

## Local development

### One-time setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Make sure `.env` exists in the project root (it already does locally).

### Start the dev server
```bash
source venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

`--reload` is required — code edits (Python and Jinja templates) hot-reload without restart.

App is at http://127.0.0.1:8000.

### Stop the dev server
```bash
pkill -f "uvicorn app.main:app"
```

---

## Database migrations (Alembic)

The app uses Alembic for Postgres schema management. SQLite (default local dev) auto-creates the schema and skips Alembic.

### Create a migration after changing models
```bash
source venv/bin/activate
alembic revision --autogenerate -m "describe the change"
# review the generated file in alembic/versions/ before applying
alembic upgrade head
```

### Apply pending migrations
```bash
alembic upgrade head
```

### Mark an existing prod DB as up-to-date (one-time)
If your prod database already has the tables (created by the old `create_all` path) and you're switching to Alembic for the first time:
```bash
alembic stamp head
```

### Roll back one migration
```bash
alembic downgrade -1
```

### Show migration status
```bash
alembic current
alembic history
```

---

## Production (Digital Ocean)

### Required env vars
```
DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST:25060/DBNAME?sslmode=require
REDIS_URL=rediss://default:PASS@HOST:25061/0
SECRET_KEY=<long random string>
OPENAI_API_KEY=...
SERPAPI_KEY=...
E2B_API_KEY=...
EMAIL_FROM=...
EMAIL_HOST_PASSWORD=...
ADMIN_EMAILS=...
LAWA_API_URL=...
ENABLE_SCHEDULER=true
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
```

`REDIS_URL` is required for multi-worker / multi-instance deployments. Without it, only one process can serve WebSockets correctly.

### First-time deploy on an existing database
```bash
alembic stamp head
```

### Every subsequent deploy
```bash
alembic upgrade head
gunicorn -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8000 app.main:app
```

`-w 4` is safe once `REDIS_URL` is configured. The scheduler is leader-gated via a Postgres advisory lock, so duplicate scout runs cannot happen.

### Health check endpoint
```
GET /healthz  →  200 {"status":"ok"}
```
Wire this into the DO Load Balancer / App Platform health probe.

### Splitting API processes from a dedicated scheduler (optional)
If you prefer a single dedicated worker process running the scheduler:
- On API processes: `ENABLE_SCHEDULER=false`
- On the worker process: `ENABLE_SCHEDULER=true`

The advisory lock makes this optional; it's purely organizational.

---

## Troubleshooting

### "Address already in use" on port 8000
```bash
pkill -f "uvicorn app.main:app"
```

### WebSockets work for some users but not others (in production)
Confirm `REDIS_URL` is set on every process and that the Redis client connects on startup — look for `Redis pub/sub connected` in the logs.

### Scheduler running scouts twice
Only possible if `DATABASE_URL` is SQLite or the advisory lock isn't being acquired. Check logs for `Scheduler tick skipped — another instance is the leader` on the non-leader processes.

### Reset local SQLite DB
```bash
rm scouts.db
# server recreates it on next start
```
