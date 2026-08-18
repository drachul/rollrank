# RollRank agent guide

## Project scope

The active application is `app/`, a local-first marble tournament manager called RollRank.

## Architecture

- `app/app.py`: Flask routes, API validation, and assembled application state.
- `app/db.py`: SQLite schema, transactions, standings, schedule balancing, and final seeding.
- `app/report.py`: ReportLab PDF generation and printable result labels.
- `app/static/app.js`: dependency-free client rendering and interactions.
- `app/static/styles.css`: responsive workspace, dashboard, final, and kiosk styling.
- `app/tests/test_app.py`: API, scheduling, scoring, UI-contract, and PDF tests.
- `Dockerfile` and `docker-compose.yml`: the supported root-level build and runtime entry points.

## Product language and rules

- Say **tournament**, **racer**, **round**, **heat**, and **final** in user-facing text.
- Each tournament owns independent racers, settings, schedule, heat results, standings, and final.
- Heat DNFs may repeat and always score zero points.
- Final positive places are unique and consecutive and must include exactly one first-place finisher.
- Multiple final DNFs are tied at the next place after the finishers. Example: `1, 2, DNF, DNF` means both DNFs tie for third.
- The final always uses one marble per racer; `marbles_per_racer` applies only to heats.
- `max_marbles_per_heat` determines the largest complete racer field that fits the configured marbles per racer.

## Change discipline

- Keep the regular dashboard, kiosk dashboard, Final tab, API state, and PDF report consistent when result semantics change.
- Keep the tournament hub status and workspace dashboard status derived from the same lifecycle meaning.
- Preserve mobile layouts and touch targets when changing shared UI.
- Escape racer and tournament names before inserting them into HTML.
- Use parameterized SQLite queries and the existing `transaction()` helper for writes.
- Prefer the existing architecture; do not add a frontend framework or extra service for a small change.

## Data safety

Tournament database data lives in root `data/`, bind-mounted to container `/data`. The source directory `app/data/` is reserved for other application data. Rebuilding or recreating the container is safe; deleting root `data/` is destructive.

- Never delete or replace root `data/` unless the user explicitly asks to erase all tournaments.
- Do not reset or replace the live database to make tests pass.
- Run tests with `APP_DATA_DIR` pointed at temporary container storage.
- Schema changes require compatibility work unless the user explicitly authorizes a fresh database.

## Verification

Run Docker commands from the repository root. Run the JavaScript syntax check against the application path.

```bash
node --check app/static/app.js
docker compose build
docker compose run --rm -e APP_DATA_DIR=/tmp/rollrank-test-data rollrank python -m unittest discover -s tests -v
docker compose up -d --force-recreate
docker compose ps
```

The test suite must pass before replacing the running container. After deployment, confirm the service is healthy and the relevant route returns HTTP 200. Preserve the named database volume throughout.
