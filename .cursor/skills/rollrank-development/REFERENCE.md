# RollRank technical reference

## Runtime

- Flask serves both `/` and `/workspace` from `static/index.html`.
- The client is plain JavaScript and renders views from `/api/state`.
- Gunicorn listens on port 8080 inside the container.
- ReportLab generates tournament-specific PDF responses.
- SQLite uses WAL mode, foreign keys, a busy timeout, and explicit write transactions.
- The root Dockerfile copies `app/requirements.txt` and then the `app/` application into `/app`.
- Root `data/` is bind-mounted at `/data` for SQLite; `app/data/` remains separate for future non-database data.

## Primary files

| File | Responsibility |
| --- | --- |
| `Dockerfile` and `docker-compose.yml` | root build, runtime, health check, port, and persistent volume |
| `app/app.py` | HTTP routes, request parsing, validation, state assembly, compatibility endpoints |
| `app/db.py` | schema initialization, tournament creation, balanced schedules, standings, final synchronization |
| `app/report.py` | printable overview, heats, final page, result labels |
| `app/static/index.html` | shared hub/workspace shell and navigation |
| `app/static/app.js` | API client, URL state, view rendering, form submission, kiosk polling |
| `app/static/styles.css` | brand, desktop/mobile workspace, podium, kiosk, print-facing UI |
| `app/tests/test_app.py` | API workflows, tournament isolation, scoring, schedule balance, PDF and frontend contracts |

## State model

`build_state()` returns:

- `competition`: selected tournament settings and progress
- `tournaments`: hub and selector summaries
- `contestants`: configured racers
- `points`: points by finishing place
- `days`: rounds containing heats, entries, marbles, finishes, and points
- `standings`: rank, per-round points, wins, and total points
- `championship`: readiness, completion, seeded racers, finishes, and champion

The database retains legacy internal names such as `competitions`, `contestants`, `days`, and `championship_entries`. Keep public language modern without casually renaming persisted identifiers.

## Important API groups

- tournament list, creation, selection, and configuration
- tournament-scoped state
- heat result submission
- final result submission
- tournament PDF report
- `/health`

Prefer tournament-scoped endpoints. Compatibility endpoints may remain for existing clients but should not drive new UI work.

## Result encoding

- `null`: result not entered
- positive integer: finishing place
- `0`: DNF

For a final with `N` positive finishers, every DNF is displayed as tied for place `N + 1`. The stored value remains `0`; derive the display placement rather than rewriting DNF rows.

## Configuration effects

Changes to rounds, heat appearances, marble limits, marbles per racer, racer roster, or final size can require rebuilding the schedule and clearing dependent results. Surface destructive consequences before applying configuration changes.

Points edits affect scoring. Verify heat totals, round totals, standings, qualifiers, dashboard summaries, and reports after changes.

## Testing notes

Tests set `APP_DATA_DIR` to temporary storage before importing the Flask app. Preserve that import order when adding tests. End-to-end API tests should create or use isolated tournament fixtures and must not depend on the live Docker volume.
