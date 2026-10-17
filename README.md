# Arbox auto-register bot

Books chosen HYPR/Arbox classes the moment registration opens.
Sun/Mon classes open the preceding Thursday (same clock time); all others 48h before.

## Try it (no Arbox needed)
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    ARBOX_MOCK=1 UI_PASSWORD=pw .venv/bin/uvicorn app.main:app
    # open http://localhost:8000  (user: admin / pw)

## Real use
1. Capture the app's API with mitmproxy (login, schedule, book) and fill in the
   `TODO(capture)` spots in `app/arbox_client.py` + `ARBOX_BASE_URL` in `.env`.
2. `cp .env.example .env`, fill it in, then `docker compose up -d --build` on a cloud VM
   (enable NTP; set TZ).
3. Reach the GUI over HTTPS (Caddy) or Tailscale - never plain HTTP on the internet.

## Layout
- `app/opening.py` opening-time rules (tested)
- `app/engine.py` 30s tick; wakes before opening, retries ~2 min, waitlist fallback, ntfy notifications
- `app/main.py` + `app/static/index.html` GUI/API for rules
- `app/arbox_client.py` the only file that talks to Arbox

Note: automated booking may violate Arbox/studio terms; use your own account, low request volume.
