# smartalerts

Auto-extracts placement / hiring deadlines from Gmail and creates Google Calendar events with multi-stage reminders, so you never miss an application window.

A personal Python bot that polls Gmail every 15 minutes, uses Google Gemini to parse unstructured recruiter emails into structured fields (company, role, CTC, deadline, eligibility, link), and publishes the result as Google Calendar events with email + popup reminders at 24h / 6h / 1h / 15min before the deadline.

## How it works

```
                    ┌──────────────────────┐
                    │ Gmail (your inbox)   │
                    └──────────┬───────────┘
                               │  is:unread newer_than:7d
                               │  + placement keywords
                               ▼
                    ┌──────────────────────┐
                    │ MIME body extractor  │  recursive walk,
                    │ (text/plain or HTML) │  base64 decode
                    └──────────┬───────────┘
                               │  full email body
                               ▼
                    ┌──────────────────────┐
                    │ Gemini 2.5 Flash Lite│  JSON-mode prompt
                    │ structured extraction│  {company, role, deadline, ...}
                    └──────────┬───────────┘
                               │
                ┌──────────────┴──────────────┐
                │                             │
                ▼                             ▼
        ┌────────────────┐           ┌────────────────────┐
        │  SQLite dedupe │           │  Google Calendar   │
        │  (placements.db)│          │  event + reminders │
        └────────────────┘           └────────────────────┘
```

Polled by `schedule` every 15 minutes. Idempotent — every email's Gmail ID is recorded in SQLite so the bot never double-processes or double-creates calendar events.

## Setup

```bash
git clone https://github.com/Pulkit2903/smartalerts.git
cd smartalerts
pip install -r requirements.txt
```

You need two things from Google before running:

**1. Gemini API key** — [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey). Free tier (`gemini-2.5-flash-lite` = 1000 RPD) is plenty.

**2. OAuth credentials for Gmail + Calendar:**
- [console.cloud.google.com](https://console.cloud.google.com) → new project → enable **Gmail API** and **Google Calendar API**
- OAuth consent screen → External → add yourself as test user
- Credentials → Create OAuth client ID → **Desktop app** → download JSON
- Save it as `credentials.json` in the repo root

Create a `.env` file:

```
GEMINI_API_KEY=your_key_here
CHECK_INTERVAL_MINUTES=15
```

Run:

```bash
python3 -u bot.py
```

First run opens a browser to authorize Gmail + Calendar scopes. Subsequent runs reuse `token.json`.

## Tech stack

- **Python 3.14**
- **Google APIs** — Gmail (read), Calendar (write), via `google-api-python-client`
- **Gemini API** — `gemini-2.5-flash-lite` for structured field extraction from email bodies
- **SQLite** — local dedupe + processed-email tracking
- **OAuth 2.0** — `google-auth-oauthlib` desktop flow with refresh-token persistence
- **schedule** — 15-min polling loop
- **python-dotenv** — config

## Files

| File | Purpose |
|---|---|
| `bot.py` | Single-file bot — auth, fetch, parse, dedupe, calendar |
| `requirements.txt` | Pinned deps |
| `.env` | (gitignored) `GEMINI_API_KEY`, `CHECK_INTERVAL_MINUTES` |
| `credentials.json` | (gitignored) Google OAuth client secrets |
| `token.json` | (gitignored, auto-generated) cached OAuth tokens |
| `placements.db` | (gitignored, auto-generated) SQLite dedupe + history |

## Status & limitations

- **Local only.** Runs as a foreground Python process; if your machine sleeps or shuts down, the bot pauses. 24/7 deployment via `launchd` / Fly.io / a cheap VPS is straightforward but not yet wired up.
- **English-only Gmail queries.** The keyword filter targets words like `placement`, `recruitment`, `hiring`, `internship`, `drive`. Localised inboxes would need the query tweaked.
- **AI is best-effort.** Gemini occasionally returns no deadline for emails that clearly contain one, and vice versa. The bot skips events with missing or past deadlines rather than guessing.
- **OAuth consent screen is in "Testing" mode**, so the app must be re-verified after 7 days unless published. Fine for personal use.

## Why I built it

College placement season produces dozens of recruiter emails per week, each with a different deadline buried in different paragraphs of HTML. Manually tracking them in a calendar is tedious and unreliable. This bot replaces that workflow with a 15-minute background loop and a phone notification before every deadline.
