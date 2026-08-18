---
name: rollrank-development
description: Develops, diagnoses, and verifies the RollRank marble tournament web app. Use for tournament configuration, balanced heat schedules, racer scoring, DNF rules, finals, dashboards, kiosk display, responsive UI, PDF reports, SQLite persistence, Docker builds, or RollRank tests.
---

# RollRank development

Application source lives in `app/`; Docker entry points and persistent database storage live at the repository root. Read the repository-root `AGENTS.md` first, then inspect only the files involved in the request. Use [REFERENCE.md](REFERENCE.md) when route, state, schema, or file ownership details are needed.

## Workflow

1. Identify the authoritative layer:
   - validation and state shape: `app.py`
   - persistence, standings, scheduling, seeding: `db.py`
   - browser rendering and interactions: `static/app.js`
   - responsive appearance: `static/styles.css`
   - printable output: `report.py`
2. Trace the behavior across every consumer before editing.
3. Make the smallest coherent change in the existing architecture.
4. Add or update regression coverage in `tests/test_app.py`.
5. Validate in Docker with temporary data.
6. Rebuild and recreate the service only after tests pass.

## Consistency checks

When changing tournament lifecycle or scoring rules, check all applicable surfaces:

- API validation and `/api/state`
- tournament hub status
- regular dashboard
- fullscreen kiosk dashboard
- Heats or Final editor
- standings and podium labels
- printable PDF report
- mobile responsive layout

Do not fix a result label in only one renderer. Derive shared concepts—such as a DNF tie position—from the same rule everywhere.

## Domain invariants

- A tournament's configuration and results are isolated by `competition_id`.
- Empty databases contain no tournament; users create the first one from the hub.
- Schedules target the configured appearances per racer per round while balancing pairwise opponents as evenly as possible.
- Heat places are positive, unique, and consecutive; repeated DNF values are allowed and score zero.
- Heat points for a racer are the sum of all that racer's marbles in the heat.
- The final is seeded from completed heat standings and uses one marble per racer.
- A completed final has exactly one first-place finisher. Other positive places are unique and consecutive.
- Final DNFs may repeat and tie at `number of positive finishers + 1`.
- A podium shows only earned first, second, and third places. Omit unavailable podium places.

## UI conventions

- Use the RollRank dark navy, blue, and yellow visual language already in `styles.css`.
- Preserve the tournament selector, PDF action, bottom mobile navigation, and kiosk entry/exit controls.
- Keep controls usable at 540 px and narrower; avoid fixed widths that force horizontal scrolling.
- Use existing marble, status, panel, podium, and button patterns before adding new components.
- Escape user-controlled text with `escapeHtml()` in template strings.
- Keep reduced-motion behavior for celebratory animation.

## Data and Docker safety

The live SQLite database is stored in root `data/` and mounted at container `/data`. The source directory `app/data/` is reserved for other application data.

- Container rebuilds and recreations are safe.
- Deleting or replacing root `data/` is not safe and requires explicit user authorization.
- Do not point tests at `/data`.
- Do not use a live tournament as a test fixture.

Run from the repository root:

```bash
node --check app/static/app.js
docker compose build
docker compose run --rm -e APP_DATA_DIR=/tmp/rollrank-test-data rollrank python -m unittest discover -s tests -v
docker compose up -d --force-recreate
docker compose ps
```

If Python dependencies are unavailable on the host, do not install them system-wide; use the Docker image.

## Completion criteria

- Requested behavior works in every applicable surface.
- New edge cases have regression coverage.
- JavaScript syntax and the full Dockerized test suite pass.
- The container reports healthy after deployment.
- Existing tournament data remains intact.
