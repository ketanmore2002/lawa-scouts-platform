# LAWA Scouts

**Automated web research, delivered as structured reports.**

LAWA Scouts is an AI-powered research platform that continuously monitors the web for topics you care about. Define a scout — a topic like "AI startups raising Series A" or "remote Rust developer jobs in Europe" — and the platform dispatches multiple AI agents in parallel to search, compile, and structure findings into an interactive report with charts, tables, filters, and downloadable exports.

## Demo

https://github.com/ketanmore2002/lawa-scouts-platform/releases/download/v1.0.0/demo.mp4

---

## The Problem

Staying on top of a fast-moving topic — job markets, competitor launches, product pricing, emerging research — means opening dozens of tabs, skimming articles, copy-pasting into spreadsheets, and doing it all over again next week. Most "alert" tools send you a wall of links with no synthesis.

**LAWA Scouts automates the entire loop:** search → compile → structure → analyse → alert → repeat on a schedule.

---

## How It Works

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  User gives  │────▶│  Strategy Engine  │────▶│  Parallel Subagents │
│  a topic     │     │  picks approach   │     │  (3–5 AI agents)    │
└─────────────┘     └──────────────────┘     └────────┬────────────┘
                                                       │
                    ┌──────────────────┐               │ each agent searches
                    │  Merge & Dedup   │◀──────────────┘ from a different angle
                    │  + AI Synthesis  │
                    └───────┬──────────┘
                            │
              ┌─────────────▼──────────────┐
              │   Structured Report        │
              │  • Interactive table       │
              │  • Auto-inferred charts    │
              │  • Stats cards & insights  │
              │  • Downloadable exports    │
              │  • Change detection        │
              └────────────────────────────┘
```

### 1. Scout Creation
You define **what** to track (topic, keywords, source filters) and **how often** (one-time, hourly, daily, weekly). A scout can belong to a personal dashboard or a shared workspace.

### 2. Multi-Agent Research
When a scout runs, the platform:
- **Detects a search strategy** — product queries route through SerpAPI Google Shopping; everything else fans out to OpenAI's web-search-enabled models.
- **Generates subtasks** — GPT breaks the topic into 3–5 complementary search angles (news sites, company pages, forums, job boards, etc.).
- **Dispatches parallel agents** — each subtask runs as an independent AI agent with web search, producing structured JSON.
- **Merges & deduplicates** — results from all agents are unified, deduped by name, and enriched with AI-generated insights.

### 3. Structured Reports
Every report includes:
- **Summary & stats** — executive summary, key metrics (items found, sources used).
- **Interactive data table** — sortable columns, full-text search, badge/tag/link column types, dropdown filters.
- **Auto-inferred charts** — the platform scans the data and renders Chart.js visualizations (categorical bar charts, time-series lines, numeric histograms) with hover tooltips, clickable legend toggles, and live filter sync.
- **Key insights** — AI-generated bullet points highlighting patterns.
- **Change detection** — when a scout runs again, AI compares the new report against the previous one and flags what actually changed (new items, removals, updates), ignoring noise from different search sources.
- **Export artifacts** — PDF, Excel, HTML, CSV, PowerPoint, plain text (via E2B sandbox or local fallback).

### 4. Collaboration
- **Workspaces** — invite team members (viewer / editor / admin roles), share scouts, and see a unified activity feed.
- **Collections** — bookmark reports across scouts into curated collections.
- **Comments & reactions** — discuss findings inline on any report. Real-time updates via WebSocket.
- **Highlights** — select and save important text snippets from reports.
- **Notifications** — in-app notification bell for scout completions, workspace invites, comments, reactions.

### 5. Scheduling & Monitoring
A built-in scheduler re-runs active scouts on their configured interval. In multi-worker deployments, a Postgres advisory lock ensures only one process runs the scheduler (leader election). An admin dashboard shows user growth, scout creation trends, report volume, and traffic origins.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3.10+, FastAPI, SQLAlchemy 2.0 (async), Pydantic |
| **Database** | PostgreSQL (production) / SQLite (local dev) |
| **AI** | OpenAI Responses API with web search, SerpAPI for product queries |
| **Frontend** | Alpine.js, Chart.js 4, vanilla CSS (dark/light themes) |
| **Real-time** | WebSockets (FastAPI native), Redis pub/sub for multi-worker fan-out |
| **Sandboxing** | E2B for chart generation and file exports (optional) |
| **Auth** | LAWA platform OAuth, JWT cookies |
| **Migrations** | Alembic |
| **Deployment** | Gunicorn + Uvicorn workers, Nginx reverse proxy, systemd |

---

## Project Structure

```
app/
├── main.py              # FastAPI app, lifespan, middleware
├── config.py            # Pydantic settings (loads from .env)
├── database.py          # SQLAlchemy async engine + session
├── models.py            # User, Scout, Report, Workspace, Collection, etc.
├── routers/
│   ├── auth.py          # Login, session, current-user
│   ├── scouts.py        # CRUD + run + run-stream (SSE)
│   ├── reports.py       # Report retrieval, sharing, exports
│   ├── workspaces.py    # Workspace CRUD, invitations, members
│   ├── collections.py   # Bookmark collections
│   ├── comments.py      # Threaded comments on reports
│   ├── highlights.py    # Text highlight snippets
│   ├── reactions.py     # Emoji reactions
│   ├── notifications.py # Notification CRUD + unread count
│   ├── realtime.py      # WebSocket endpoint
│   ├── activity.py      # Activity feed
│   ├── admin.py         # Admin stats, charts, user management
│   └── pages.py         # HTML page routes (SSR templates)
├── services/
│   ├── scout_runner.py  # Multi-agent orchestration engine
│   ├── sandbox_runner.py# E2B sandbox for charts & exports
│   ├── ws_hub.py        # WebSocket pub/sub hub (local + Redis)
│   ├── scheduler.py     # Cron-like scout scheduler with leader lock
│   ├── auth.py          # LAWA API verification, JWT
│   ├── email_service.py # Email report delivery
│   ├── notification_service.py
│   ├── presence.py      # Online presence tracking
│   └── browser_agent.py # Headless browser fallback
├── static/
│   ├── app.css          # Full stylesheet (dark + light themes)
│   ├── app.js           # API client, notifications, theme toggle
│   ├── router.js        # SPA partial-page navigation
│   └── ws.js            # WebSocket client
└── templates/           # Jinja2 + Alpine.js templates (15 pages)
```

---

## Quick Start

### Prerequisites
- Python 3.10+
- An OpenAI API key (with Responses API access)
- A LAWA platform account (for authentication)

### Setup

```bash
# Clone
git clone https://github.com/ketanmore2002/lawa-scouts-platform.git
cd lawa-scouts-platform

# Virtual environment
python3 -m venv venv
source venv/bin/activate

# Dependencies
pip install -r requirements.txt

# Environment
cp .env.example .env
# Edit .env — at minimum set OPENAI_API_KEY
```

### Configure `.env`

```env
OPENAI_API_KEY=sk-...          # Required — OpenAI API key
DATABASE_URL=sqlite+aiosqlite:///./scouts.db   # Default for local dev
SECRET_KEY=change-me           # JWT signing key
ADMIN_EMAILS=you@example.com   # Comma-separated admin emails
SERPAPI_KEY=                    # Optional — enables product search
E2B_API_KEY=                   # Optional — enables chart/export generation
REDIS_URL=                     # Optional — required for multi-worker WebSockets
```

### Run

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and log in with your LAWA credentials.

---

## Production Deployment

See [PRODUCTION.md](PRODUCTION.md) for the full deployment guide including:
- Gunicorn + Uvicorn worker configuration
- Nginx reverse proxy with WebSocket support
- PostgreSQL + Alembic migrations
- Redis setup for multi-worker pub/sub
- systemd service files
- Worker sizing guidelines

---

## Key Features at a Glance

- **Multi-agent parallel search** — 3–5 AI agents research your topic simultaneously from different angles
- **Structured reports** — not just links, but tables with sortable columns, filters, and search
- **Interactive charts** — auto-inferred from report data (bar, line, histogram) with tooltips and legend toggles
- **Change detection** — AI compares consecutive runs and tells you what actually changed
- **Real-time collaboration** — WebSocket-powered comments, reactions, highlights, presence indicators
- **Scheduled monitoring** — set it and forget it; scouts re-run on your chosen interval
- **Export anything** — PDF, Excel, HTML, CSV, PowerPoint, plain text
- **Dark & light themes** — system-aware, instantly toggleable
- **Admin dashboard** — user growth charts, scout creation trends, traffic origins, user management

---

## License

MIT
