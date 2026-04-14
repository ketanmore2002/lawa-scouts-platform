# Production settings — Digital Ocean

Reference for deploying LAWA Scouts on Digital Ocean (Droplet or App Platform). For local dev commands see `RUNNING.md`.

---

## 1. Infrastructure shopping list

| Component | DO product | Smallest sane tier | Notes |
|---|---|---|---|
| App | Droplet **or** App Platform | 2 vCPU / 4 GB | App Platform = simpler; Droplet = cheaper + flexible |
| Database | Managed PostgreSQL | 1 GB / 1 vCPU ($15/mo) | Bump to 2 GB if connection limit (22) is too tight |
| Cache / pub-sub | Managed Redis | 1 GB ($15/mo) | **Required** for >1 worker |
| TLS / DNS | DO LB or Caddy/Nginx | — | App Platform handles TLS automatically |
| Object storage (optional) | DO Spaces | $5/mo | If you outgrow local static serving |

Total starter cost: ~$50–70/mo.

---

## 2. Environment variables

Set these in App Platform's "App-Level Env Vars" panel, or in `/etc/scouts.env` on a Droplet.

### Required
```
DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST:25060/DBNAME?sslmode=require
REDIS_URL=rediss://default:PASS@HOST:25061/0
SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_urlsafe(64))">
OPENAI_API_KEY=sk-...
SERPAPI_KEY=...
E2B_API_KEY=...
EMAIL_FROM=contact@yourdomain.com
EMAIL_HOST_PASSWORD=...
ADMIN_EMAILS=you@yourdomain.com
LAWA_API_URL=https://api.yourdomain.com
BASE_URL=https://yourdomain.com
```

### Scaling / pool tuning
```
ENABLE_SCHEDULER=true       # set to false on API-only processes if you split out a worker
DB_POOL_SIZE=5              # per worker — keep total under PG max_connections
DB_MAX_OVERFLOW=5           # per worker
```

### Mark as **secret** in App Platform
Anything containing a key, password, or token — that hides them in the UI and logs.

---

## 3. Worker sizing

For async FastAPI workers (NOT the classic `2*CPU+1` rule):

| Droplet / Instance | vCPUs | RAM | Workers (`-w`) |
|---|---|---|---|
| Basic 1/1GB | 1 | 1 GB | **1** |
| Basic 1/2GB | 1 | 2 GB | **1** |
| Basic 2/2GB | 2 | 2 GB | **2** |
| Basic 2/4GB | 2 | 4 GB | **2** |
| Basic 4/8GB | 4 | 8 GB | **3–4** |
| App Platform Basic XS (1 vCPU / 512 MB) | 1 | 0.5 GB | **1** |
| App Platform Pro M (2 vCPU / 4 GB) | 2 | 4 GB | **2** |

Each worker uses ~150–250 MB. Each worker holds up to `DB_POOL_SIZE + DB_MAX_OVERFLOW` Postgres connections.

### Connection math
```
total_pg_connections = workers × instances × (DB_POOL_SIZE + DB_MAX_OVERFLOW)
```
Stay under your Managed PG `max_connections` (22 on the smallest plan).

Example: 2 workers × 1 instance × (5 + 5) = 20 connections. ✅ Fits.

---

## 4. Run command

### Gunicorn (recommended)
```bash
gunicorn -k uvicorn.workers.UvicornWorker \
  -w 2 \
  -b 0.0.0.0:8000 \
  --timeout 120 \
  --graceful-timeout 30 \
  --keep-alive 5 \
  --access-logfile - \
  --error-logfile - \
  app.main:app
```

`--timeout 120` is needed because `/api/scouts/{id}/run-stream` can take a long time.

### Uvicorn (alternative, no gunicorn dep)
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2 --log-level info
```

### Never use `--reload` in prod.

---

## 5. Deploy steps

### First-time deploy (existing DB with tables already created)
```bash
pip install -r requirements.txt
alembic stamp head     # tells Alembic the DB is already at latest schema
```

### Every subsequent deploy
```bash
pip install -r requirements.txt
alembic upgrade head   # apply any new migrations
# then start gunicorn (or restart systemd unit / App Platform redeploy)
```

In App Platform, put `pip install -r requirements.txt && alembic upgrade head` in the **build** or **pre-deploy** command.

---

## 6. Reverse proxy / load balancer

WebSockets need correct upgrade headers and long timeouts.

### Nginx snippet
```nginx
upstream scouts {
    server 127.0.0.1:8000;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com;

    # ... SSL config ...

    client_max_body_size 25m;

    location / {
        proxy_pass http://scouts;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

### DO Load Balancer
- Forwarding rules: HTTPS 443 → HTTP 8000 (or HTTPS pass-through).
- **Sticky sessions: not required** (Redis pub/sub bridges workers).
- Idle timeout: bump to 3600s for WebSockets.
- Health check path: `/healthz`.

---

## 7. systemd unit (Droplet)

`/etc/systemd/system/scouts.service`:
```ini
[Unit]
Description=LAWA Scouts
After=network.target

[Service]
Type=simple
User=scouts
WorkingDirectory=/opt/scouts
EnvironmentFile=/etc/scouts.env
ExecStart=/opt/scouts/venv/bin/gunicorn \
  -k uvicorn.workers.UvicornWorker \
  -w 2 -b 0.0.0.0:8000 \
  --timeout 120 --graceful-timeout 30 \
  app.main:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable scouts
sudo systemctl start scouts
sudo systemctl status scouts
journalctl -u scouts -f      # tail logs
```

---

## 8. Health check

```
GET /healthz  →  200 {"status":"ok"}
```

Use as the LB / App Platform / uptime monitor probe path.

---

## 9. Scaling decisions

Add capacity in this order:

1. **Bump workers** until RAM reaches ~70 %. Watch with `htop` / DO graphs.
2. **Increase Postgres tier** if connection cap hits or queries slow. Verify with `SELECT count(*) FROM pg_stat_activity;`.
3. **Add a second app instance** behind the LB. Redis pub/sub already handles WS fan-out across instances.
4. **Split scheduler** to its own process: one box with `ENABLE_SCHEDULER=true`, all others with `ENABLE_SCHEDULER=false`. Optional — the Postgres advisory lock makes duplicate runs impossible regardless.
5. **CDN for static assets** (DO Spaces + CDN) once `/static/` egress matters.

---

## 10. Observability

### Minimum
- `journalctl -u scouts -f` (Droplet) or App Platform Runtime Logs.
- DO Insights graphs: CPU, RAM, bandwidth.
- `SELECT * FROM pg_stat_activity;` to spot connection / query problems.

### Recommended additions
- **Sentry** — drop the SDK in `app/main.py`, set `SENTRY_DSN` env var.
- **Uptime monitor** — Better Stack / UptimeRobot pinging `/healthz` every 60 s.
- **Postgres slow-query log** — enable in DO console; review weekly.

---

## 11. Backups

- DO Managed Postgres: daily backups, 7-day retention by default. Verify in console.
- App code: git tags per release. Tag before each `alembic upgrade head`.
- Static uploads (if/when added): DO Spaces versioning.

---

## 12. Pre-launch checklist

- [ ] `SECRET_KEY` rotated (NOT the placeholder)
- [ ] `DATABASE_URL` points at Managed PG with `sslmode=require`
- [ ] `REDIS_URL` set
- [ ] `ENABLE_SCHEDULER=true` on exactly one process group
- [ ] `DB_POOL_SIZE × workers × instances < pg_max_connections`
- [ ] `alembic upgrade head` succeeds
- [ ] `/healthz` returns 200
- [ ] LB / Nginx forwards `Upgrade` header and idle timeout ≥ 3600 s
- [ ] WebSockets work in browser dev tools (Network → WS tab)
- [ ] Logs reach `journalctl` / App Platform
- [ ] Sentry / uptime monitor active
- [ ] Custom domain + TLS green
- [ ] CORS `allow_origins` tightened to your real domain (currently `*` in `app/main.py`)
