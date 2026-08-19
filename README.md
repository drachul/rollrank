# RollRank

A Dockerized marble tournament workspace for configuring fair heats, scoring every marble, ranking racers, running finals, and exporting polished reports.

## Project structure

- `Dockerfile` and `docker-compose.yml`: build and run RollRank from the repository root.
- `app/`: Flask API, SQLite scheduling and scoring, browser UI, PDF reports, and tests.
- `data/`: persistent SQLite database data mounted into the container at `/data`.
- `app/data/`: reserved for future non-database application data.
- `AGENTS.md` and `.cursor/`: project guidance for AI coding agents.

## Run

From the repository root:

```bash
docker compose up --build
```

Open <http://localhost:7272>. Tournament data is stored in `./data/rollrank.db` and survives image rebuilds and container recreation.

Stop the app without deleting tournament data:

```bash
docker compose down
```

Stopping or recreating the container does not remove `./data`. Back up that directory before intentionally deleting or replacing the database.

## Included workflows

- Start from an empty database and create the first tournament from the tournament hub.
- See every tournament's heat progress and live points leader from the index page.
- Create and switch between tournaments with independent settings and results.
- Configure tournament name, rounds, heats per racer per round, maximum marbles per heat, marbles per racer, scoring, and final size.
- Automatically choose the largest complete racer field that fits the per-heat marble limit.
- Generate exact racer appearances while balancing pairwise matchups.
- Enter or correct each marble's heat result, including zero-point DNFs.
- Seed and score a final with multiple tied DNFs when necessary.
- Use the responsive dashboard or fullscreen live kiosk display.
- Open or print a tournament-specific database-generated PDF report.

## Tests

Run the complete suite in an isolated container from the repository root:

```bash
docker compose run --rm -e APP_DATA_DIR=/tmp/rollrank-test-data rollrank python -m unittest discover -s tests -v
```
