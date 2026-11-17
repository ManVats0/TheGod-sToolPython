# The God's Tool: Python service

FastAPI service behind app.thegodstool.com. It reads data sources, finds problems, runs cleaning pipelines, talks to the AI, and serves the free try and the website chat.

## Run it
```
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
Put local settings in `credentials.env` (loaded automatically, never commit it).

## Files
| File | What it does |
| --- | --- |
| `main.py` | All routes and helpers. A map of sections is at the top of the file. |
| `free_try.py` | Limits and logic for the no-account free try (nothing stored). |
| `pdf_statement.py`, `pdf_tables.py` | Reading bank statements and PDF tables, no AI. |
| `site_chat.py` | Website support chat, answers from the public site text only. See `SITE_CHAT.md`. |
| `agent.py` | The tunnel agent customers install to reach databases behind a firewall. |

## Settings (environment variables)
`MONGODB_URI`, `JWT_SECRET`, `CRED_ENCRYPTION_KEY`, `FRONTEND_URL`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SMTP_EMAIL`, `SMTP_PASSWORD`, `RESEND_API_KEY`, `ANTHROPIC_API_KEY`, `ADMIN_EMAILS`, `AI_PRIVACY_MODE`, `AI_ALLOW_SHARING`, `PLAN_LOCKS`, `ENFORCE_PLAN_EXPIRY`, `ENFORCE_SEAT_LIMITS`, `SESSION_CHECK`, `SESSION_CHECK_TTL_SECONDS`, `SITE_CHAT`, `SITE_CHAT_MODEL`, `SITE_CHAT_DAILY_LIMIT`, `KNOWLEDGE_URL`.

## Code style
Formatted with `black -l 120`. Run `black -l 120 *.py` before committing.
