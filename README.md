# Survival Royale — Backend (INFINITO 2026)

FastAPI + SQLite. Same look as your original page, but:
- Answer key stays on the server (not in page source); review is shown only after submit
- Server-side timer (same limit for all, survives refresh, auto-submits on expiry)
- Autosave every answer; reconnect/refresh resumes the attempt
- Per-attempt shuffled order, one attempt per name+arena, optional access code
- Tab-switch counter, leaderboard (score, then faster time), CSV export
- Admin dashboard at /admin (HTTP Basic auth)

## Run
    pip install -r requirements.txt
    ADMIN_PASSWORD=choose-a-strong-one uvicorn app:app --host 0.0.0.0 --port 8000

Players: http://<server>:8000/     Admin: http://<server>:8000/admin (user: admin)

Env vars: ADMIN_PASSWORD (required for admin), ADMIN_USER, DB_PATH.

## Deploy (free-ish)
Render/Railway/Fly: start command above. Use a persistent disk for DB_PATH,
and run a SINGLE worker (SQLite + in-process lock). Put it behind HTTPS.

## Test
    python3 test_api.py
