# Arbox auto-register bot

Books chosen HYPR/Arbox classes the moment registration opens.
Each class's opening time comes from Arbox itself (`enable_registration_time`: 48h for most
classes, 72h for Sunday and most Monday classes).

## Try it (no Arbox needed)
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    ARBOX_MOCK=1 UI_PASSWORD=pw .venv/bin/uvicorn app.main:app
    # open http://localhost:8000  (user: admin / pw)

## Real use
1. `cp .env.example .env`, fill in `ARBOX_EMAIL`, `ARBOX_PASSWORD`, `UI_PASSWORD`. Keep `DRY_RUN=1` first.
2. Deploy to a cloud VM: follow **DEPLOY.md** (step by step, with a safe dry-run rehearsal).

API notes (from an iOS traffic capture): `POST /user/login` -> `accesstoken`/`refreshtoken` headers;
`POST /schedule/betweenDates` (one day per call); `POST /scheduleUser/insert` to book;
`POST /scheduleStandBy/insert` for the waitlist; HTTP 425 `registerScheduleDisabled` = not open yet.

## Layout
- `app/opening.py` opening time = start - Arbox's per-class registration hours (tested)
- `app/engine.py` hourly tick; wakes before opening, retries ~2 min, waitlist fallback, ntfy notifications
- `app/main.py` + `app/static/index.html` GUI/API for rules
- `app/arbox_client.py` the only file that talks to Arbox

Note: automated booking may violate Arbox/studio terms; use your own account, low request volume.

## Tests
    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/pytest

54 tests: API client against a fake Arbox server shaped like the captured traffic
(`tests/fake_arbox.py`), engine end-to-end (book, retry, waitlist, failure, no double-booking),
SQLite, and the web API/GUI. Nothing in the suite contacts the real Arbox.
