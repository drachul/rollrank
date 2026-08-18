from __future__ import annotations

import os
import random
import sqlite3
from collections import Counter
from contextlib import contextmanager
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence


DEFAULT_RACERS = [
    ("Ruby Rocket", "#EF5A5A"),
    ("Blue Bolt", "#2F80ED"),
    ("Golden Globe", "#F2B134"),
    ("Emerald Flash", "#27AE60"),
    ("Purple Comet", "#9B51E0"),
    ("Orange Orbit", "#F2994A"),
    ("Silver Streak", "#00A6A6"),
    ("Pink Lightning", "#E056A7"),
]
DEFAULT_POINTS = [10, 7, 5, 3, 2, 1]

MAX_RACERS_PER_HEAT = 24

CHAMPIONSHIP_STAGES = ("wildcard", "preliminary", "final")
STAGE_CASCADE = {
    "wildcard": ("wildcard", "preliminary", "final"),
    "preliminary": ("preliminary", "final"),
    "final": ("final",),
}


def database_path() -> Path:
    data_dir = Path(os.environ.get("APP_DATA_DIR", Path(__file__).parent.parent / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "marble_race.db"


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(database_path(), timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# NOTE: this schema is a breaking change from earlier versions (stage-aware heats,
# no final_entries table, renamed/new tournament columns). CREATE TABLE IF NOT
# EXISTS does not add columns to an already-created table, so an existing
# data/marble_race.db built under the old schema must be deleted (or
# APP_DATA_DIR pointed at a fresh directory) before running against this schema.
SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS tournaments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        days INTEGER NOT NULL CHECK (days >= 1),
        heats_per_day INTEGER NOT NULL CHECK (heats_per_day >= 1),
        heats_per_racer_per_day INTEGER NOT NULL DEFAULT 3 CHECK (heats_per_racer_per_day >= 1),
        racers_per_heat INTEGER NOT NULL CHECK (racers_per_heat >= 2),
        max_marbles_per_heat INTEGER NOT NULL CHECK (max_marbles_per_heat >= 2),
        marbles_per_racer INTEGER NOT NULL DEFAULT 1 CHECK (marbles_per_racer >= 1),
        championship_max_marbles_per_heat INTEGER NOT NULL DEFAULT 6 CHECK (championship_max_marbles_per_heat >= 2),
        max_bye_marbles_per_racer INTEGER NOT NULL DEFAULT 1 CHECK (max_bye_marbles_per_racer >= 0),
        max_final_racers INTEGER NOT NULL CHECK (max_final_racers >= 2),
        seed INTEGER NOT NULL DEFAULT 7,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS racers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        color TEXT NOT NULL,
        sort_order INTEGER NOT NULL,
        UNIQUE (tournament_id, name COLLATE NOCASE),
        UNIQUE (tournament_id, sort_order)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS point_values (
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        place INTEGER NOT NULL CHECK (place >= 1),
        points INTEGER NOT NULL CHECK (points >= 0),
        PRIMARY KEY (tournament_id, place)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS heats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        stage TEXT NOT NULL DEFAULT 'staging' CHECK (stage IN ('staging','wildcard','preliminary','final')),
        day INTEGER,
        heat_number INTEGER NOT NULL,
        global_number INTEGER NOT NULL,
        started_at TEXT,
        UNIQUE (tournament_id, stage, day, heat_number),
        UNIQUE (tournament_id, global_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS heat_entries (
        heat_id INTEGER NOT NULL REFERENCES heats(id) ON DELETE CASCADE,
        lane INTEGER NOT NULL,
        racer_id INTEGER NOT NULL REFERENCES racers(id) ON DELETE CASCADE,
        marble_number INTEGER NOT NULL CHECK (marble_number >= 1),
        finish INTEGER,
        points INTEGER,
        origin_stage TEXT,
        origin_round INTEGER,
        origin_heat_id INTEGER REFERENCES heats(id) ON DELETE SET NULL,
        PRIMARY KEY (heat_id, lane, marble_number),
        UNIQUE (heat_id, racer_id, marble_number)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_heats_day ON heats (tournament_id, day, heat_number)",
    "CREATE INDEX IF NOT EXISTS idx_heats_stage ON heats (tournament_id, stage)",
    "CREATE INDEX IF NOT EXISTS idx_heat_entries_racer ON heat_entries (racer_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tournaments_name_nocase ON tournaments (name COLLATE NOCASE)",
]


def create_tournament(connection: sqlite3.Connection, name: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO tournaments
            (name, days, heats_per_day, heats_per_racer_per_day,
             racers_per_heat, max_marbles_per_heat, marbles_per_racer,
             championship_max_marbles_per_heat, max_bye_marbles_per_racer,
             max_final_racers, seed)
        VALUES (?, 3, 4, 3, 6, 6, 1, 6, 1, 6, 7)
        """,
        (name,),
    )
    tournament_id = int(cursor.lastrowid)
    connection.executemany(
        """
        INSERT INTO racers (tournament_id, name, color, sort_order)
        VALUES (?, ?, ?, ?)
        """,
        [
            (tournament_id, racer_name, color, index)
            for index, (racer_name, color) in enumerate(DEFAULT_RACERS)
        ],
    )
    connection.executemany(
        "INSERT INTO point_values (tournament_id, place, points) VALUES (?, ?, ?)",
        [
            (tournament_id, index, value)
            for index, value in enumerate(DEFAULT_POINTS, start=1)
        ],
    )
    rebuild_schedule(connection, tournament_id)
    return tournament_id


def init_db() -> None:
    connection = connect()
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA:
            connection.execute(statement)
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise sqlite3.IntegrityError("Tournament migration left invalid related records.")
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA optimize")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def calculate_racers_per_heat(
    racer_count: int,
    heats_per_racer: int,
    max_marbles_per_heat: int,
    marbles_per_racer: int,
) -> int:
    """Choose the largest complete heat that fits within the marble limit."""
    capacity = min(
        racer_count,
        MAX_RACERS_PER_HEAT,
        max_marbles_per_heat // marbles_per_racer,
    )
    total_appearances = racer_count * heats_per_racer
    for racers_per_heat in range(capacity, 1, -1):
        if total_appearances % racers_per_heat == 0:
            return racers_per_heat
    raise ValueError(
        "No full heat schedule fits this maximum. Increase max marbles per heat "
        "or adjust heats per racer per round."
    )


def calculate_championship_heat_size(
    field_size: int, max_marbles_per_heat: int, marbles_per_racer: int
) -> tuple[int, int]:
    """Largest single-appearance heat size that evenly divides field_size.

    Returns (0, 0) when no valid heat can be formed (field too small, or no
    divisor within the marble limit) -- callers treat that as "the whole field
    auto-advances to the next stage without racing" rather than raising.
    """
    if field_size < 2:
        return 0, 0
    try:
        racers_per_heat = calculate_racers_per_heat(field_size, 1, max_marbles_per_heat, marbles_per_racer)
    except ValueError:
        return 0, 0
    if racers_per_heat < 2:
        return 0, 0
    return racers_per_heat, field_size // racers_per_heat


def balanced_day_schedule(
    racer_ids: Sequence[int],
    race_size: int,
    heats_per_racer: int,
    existing_pair_counts: Counter[tuple[int, int]],
    seed: int,
) -> list[list[int]]:
    """Create exact appearances while minimizing pairwise opponent-count spread."""
    heat_count = len(racer_ids) * heats_per_racer // race_size
    restart_count = max(12, min(140, 5000 // max(1, heat_count * race_size)))
    all_pairs = [tuple(sorted(pair)) for pair in combinations(racer_ids, 2)]
    best_schedule: list[list[int]] | None = None
    best_score: tuple[float, float, int] | None = None

    for restart in range(restart_count):
        rng = random.Random(seed * 1009 + restart * 9176)
        remaining = {racer_id: heats_per_racer for racer_id in racer_ids}
        day_pairs: Counter[tuple[int, int]] = Counter()
        groups: list[list[int]] = []
        valid = True

        for heat_index in range(heat_count):
            heats_left = heat_count - heat_index
            mandatory = [
                racer_id for racer_id in racer_ids if remaining[racer_id] == heats_left
            ]
            if len(mandatory) > race_size:
                valid = False
                break
            group = list(mandatory)
            while len(group) < race_size:
                candidates = [
                    racer_id
                    for racer_id in racer_ids
                    if remaining[racer_id] > 0 and racer_id not in group
                ]
                if not candidates:
                    valid = False
                    break

                def candidate_score(racer_id: int) -> tuple[float, float, float]:
                    pair_cost = sum(
                        existing_pair_counts[tuple(sorted((racer_id, opponent)))] * 4
                        + day_pairs[tuple(sorted((racer_id, opponent)))] * 7
                        for opponent in group
                    )
                    urgency = -remaining[racer_id] * 2.5
                    history = sum(
                        1
                        for previous in groups
                        if racer_id in previous and len(set(previous).intersection(group)) >= max(1, race_size - 2)
                    )
                    return pair_cost + history * 5 + urgency, rng.random(), racer_id

                candidates.sort(key=candidate_score)
                choice_pool = candidates[: min(4, len(candidates))]
                choice = rng.choice(choice_pool)
                group.append(choice)
            if not valid:
                break
            rng.shuffle(group)
            for racer_id in group:
                remaining[racer_id] -= 1
            for first, second in combinations(group, 2):
                day_pairs[tuple(sorted((first, second)))] += 1
            groups.append(group)

        if not valid or any(remaining.values()):
            continue
        combined = [existing_pair_counts[pair] + day_pairs[pair] for pair in all_pairs]
        daily = [day_pairs[pair] for pair in all_pairs]
        duplicate_groups = len(groups) - len({tuple(sorted(group)) for group in groups})
        score = (
            max(combined) - min(combined),
            sum((value - sum(combined) / len(combined)) ** 2 for value in combined),
            duplicate_groups * 100 + max(daily) - min(daily),
        )
        if best_score is None or score < best_score:
            best_score = score
            best_schedule = groups

    if best_schedule is None:
        raise ValueError("Unable to build a balanced schedule for this configuration.")
    return best_schedule


def balanced_schedule(
    racer_ids: Sequence[int],
    race_size: int,
    heats_per_racer: int,
    days: int,
    seed: int,
) -> list[list[list[int]]]:
    pair_counts: Counter[tuple[int, int]] = Counter()
    schedule: list[list[list[int]]] = []
    for day in range(1, days + 1):
        day_schedule = balanced_day_schedule(
            racer_ids,
            race_size,
            heats_per_racer,
            pair_counts,
            seed + day * 37,
        )
        schedule.append(day_schedule)
        for group in day_schedule:
            for first, second in combinations(group, 2):
                pair_counts[tuple(sorted((first, second)))] += 1
    return improve_pair_balance(schedule, racer_ids, seed)


def improve_pair_balance(
    schedule: list[list[list[int]]], racer_ids: Sequence[int], seed: int
) -> list[list[list[int]]]:
    """Improve global opponent balance with within-day swaps.

    Swapping racers between two heats on the same day preserves every racer's
    exact daily appearance count and the configured heat size.
    """
    rng = random.Random(seed * 7919 + 17)
    all_pairs = [tuple(sorted(pair)) for pair in combinations(racer_ids, 2)]
    pair_counts: Counter[tuple[int, int]] = Counter()
    for day_schedule in schedule:
        for group in day_schedule:
            pair_counts.update(tuple(sorted(pair)) for pair in combinations(group, 2))

    def objective(counts: Counter[tuple[int, int]]) -> tuple[int, int]:
        values = [counts[pair] for pair in all_pairs]
        return max(values) - min(values), sum(value * value for value in values)

    current_score = objective(pair_counts)
    heat_total = sum(len(day_schedule) for day_schedule in schedule)
    iterations = min(50_000, max(5_000, heat_total * 700))
    for _ in range(iterations):
        if current_score[0] == 0:
            break
        day_schedule = rng.choice(schedule)
        if len(day_schedule) < 2:
            continue
        first_index, second_index = rng.sample(range(len(day_schedule)), 2)
        first_group = day_schedule[first_index]
        second_group = day_schedule[second_index]
        first_choices = [racer_id for racer_id in first_group if racer_id not in second_group]
        second_choices = [racer_id for racer_id in second_group if racer_id not in first_group]
        if not first_choices or not second_choices:
            continue
        first_racer = rng.choice(first_choices)
        second_racer = rng.choice(second_choices)
        new_first = [second_racer if racer_id == first_racer else racer_id for racer_id in first_group]
        new_second = [first_racer if racer_id == second_racer else racer_id for racer_id in second_group]

        candidate_counts = pair_counts.copy()
        candidate_counts.subtract(
            tuple(sorted(pair)) for pair in combinations(first_group, 2)
        )
        candidate_counts.subtract(
            tuple(sorted(pair)) for pair in combinations(second_group, 2)
        )
        candidate_counts.update(tuple(sorted(pair)) for pair in combinations(new_first, 2))
        candidate_counts.update(tuple(sorted(pair)) for pair in combinations(new_second, 2))
        candidate_score = objective(candidate_counts)
        if candidate_score < current_score or (
            candidate_score == current_score and rng.random() < 0.08
        ):
            day_schedule[first_index] = new_first
            day_schedule[second_index] = new_second
            pair_counts = candidate_counts
            current_score = candidate_score
    return schedule


def rebuild_schedule(connection: sqlite3.Connection, tournament_id: int) -> None:
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    racer_ids = [
        row["id"]
        for row in connection.execute(
            "SELECT id FROM racers WHERE tournament_id = ? ORDER BY sort_order",
            (tournament_id,),
        )
    ]
    if not tournament or len(racer_ids) < tournament["racers_per_heat"]:
        raise ValueError("Not enough racers to build the heat schedule.")
    delete_championship_stages(connection, tournament_id, "wildcard")
    connection.execute(
        "DELETE FROM heats WHERE tournament_id = ? AND stage = 'staging'", (tournament_id,)
    )
    total_slots_per_day = len(racer_ids) * tournament["heats_per_racer_per_day"]
    if total_slots_per_day % tournament["racers_per_heat"]:
        raise ValueError("The racer appearances do not divide into complete heats.")
    heats_per_day = total_slots_per_day // tournament["racers_per_heat"]
    connection.execute(
        "UPDATE tournaments SET heats_per_day = ? WHERE id = ?",
        (heats_per_day, tournament_id),
    )
    schedule = balanced_schedule(
        racer_ids,
        tournament["racers_per_heat"],
        tournament["heats_per_racer_per_day"],
        tournament["days"],
        tournament["seed"],
    )
    global_index = 0
    for day, day_schedule in enumerate(schedule, start=1):
        for heat_number, participants in enumerate(day_schedule, start=1):
            global_index += 1
            cursor = connection.execute(
                """
                INSERT INTO heats (tournament_id, stage, day, heat_number, global_number)
                VALUES (?, 'staging', ?, ?, ?)
                """,
                (tournament_id, day, heat_number, global_index),
            )
            heat_id = cursor.lastrowid
            connection.executemany(
                """
                INSERT INTO heat_entries
                    (heat_id, lane, racer_id, marble_number)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (heat_id, lane, racer_id, marble_number)
                    for lane, racer_id in enumerate(participants, start=1)
                    for marble_number in range(1, tournament["marbles_per_racer"] + 1)
                ],
            )


def standings(
    connection: sqlite3.Connection, tournament_id: int
) -> list[dict[str, Any]]:
    """Ranks racers by their placing within each staging round (1st, 2nd, ...)
    rather than by points accumulated across the whole tournament. A racer's
    rank is decided by how many rounds they placed 1st, then 2nd, then 3rd/4th,
    with the sum of their round placings (lower is better) as a final tiebreak
    before falling back to seed order.
    """
    tournament = connection.execute(
        "SELECT days FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    racers = connection.execute(
        "SELECT id, name, color, sort_order FROM racers WHERE tournament_id = ? ORDER BY sort_order ASC",
        (tournament_id,),
    ).fetchall()

    field = championship_field(connection, tournament_id)
    day_tier: dict[tuple[int, int], str] = {}
    for item in field["byes"]:
        day_tier[(item["originRound"], item["racerId"])] = "bye"
    for item in field["preliminaryDirect"]:
        day_tier.setdefault((item["originRound"], item["racerId"]), "preliminary")
    for item in field["wildcardPool"]:
        day_tier.setdefault((item["originRound"], item["racerId"]), "wildcard")

    day_placements: dict[int, list[int | None]] = {row["id"]: [] for row in racers}
    day_tiers: dict[int, list[str | None]] = {row["id"]: [] for row in racers}
    for day in range(1, tournament["days"] + 1):
        ranking = round_standings(connection, tournament_id, day)
        day_raced = any(row["totalPoints"] > 0 for row in ranking)
        for row in ranking:
            day_placements[row["id"]].append(row["rank"] if day_raced else None)
            day_tiers[row["id"]].append(day_tier.get((day, row["id"])) if day_raced else None)

    summaries = []
    for row in racers:
        placements = day_placements[row["id"]]
        raced_placements = [place for place in placements if place is not None]
        summaries.append(
            {
                "id": row["id"],
                "name": row["name"],
                "color": row["color"],
                "sortOrder": row["sort_order"],
                "wins": sum(1 for place in placements if place == 1),
                "seconds": sum(1 for place in placements if place == 2),
                "thirdFourth": sum(1 for place in placements if place in (3, 4)),
                "placementSum": sum(raced_placements),
                "dayPlacements": placements,
                "dayChampionshipTiers": day_tiers[row["id"]],
            }
        )

    summaries.sort(
        key=lambda s: (
            -s["wins"],
            -s["seconds"],
            -s["thirdFourth"],
            s["placementSum"],
            s["sortOrder"],
        )
    )
    result = []
    for rank, summary in enumerate(summaries, start=1):
        del summary["sortOrder"]
        del summary["placementSum"]
        result.append({"rank": rank, **summary})
    return result


def round_standings(
    connection: sqlite3.Connection, tournament_id: int, day: int
) -> list[dict[str, Any]]:
    """Same shape/tiebreak as standings(), scoped to a single staging day."""
    rows = connection.execute(
        """
        SELECT r.id, r.name, r.color, r.sort_order,
               COALESCE(SUM(he.points), 0) AS total_points,
               COALESCE(SUM(CASE WHEN he.finish = 1 THEN 1 ELSE 0 END), 0) AS wins
        FROM racers r
        LEFT JOIN heats h ON h.tournament_id = r.tournament_id AND h.stage = 'staging' AND h.day = ?
        LEFT JOIN heat_entries he ON he.heat_id = h.id AND he.racer_id = r.id
        WHERE r.tournament_id = ?
        GROUP BY r.id
        ORDER BY total_points DESC, wins DESC, r.sort_order ASC
        """,
        (day, tournament_id),
    ).fetchall()
    return [
        {
            "rank": rank,
            "id": row["id"],
            "name": row["name"],
            "color": row["color"],
            "totalPoints": row["total_points"],
            "wins": row["wins"],
        }
        for rank, row in enumerate(rows, start=1)
    ]


def completed_heat_count(
    connection: sqlite3.Connection, tournament_id: int, stage: str | None = None
) -> int:
    params: list[Any] = [tournament_id]
    stage_clause = ""
    if stage is not None:
        stage_clause = " AND h.stage = ?"
        params.append(stage)
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS completed
        FROM (
            SELECT h.id
            FROM heats h
            JOIN heat_entries he ON he.heat_id = h.id
            WHERE h.tournament_id = ?{stage_clause}
            GROUP BY h.id
            HAVING COUNT(*) = SUM(CASE WHEN he.finish IS NOT NULL THEN 1 ELSE 0 END)
        )
        """,
        params,
    ).fetchone()
    return row["completed"]


def is_stage_complete(connection: sqlite3.Connection, tournament_id: int, stage: str) -> bool:
    row = connection.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN he.finish IS NOT NULL THEN 1 ELSE 0 END) AS finished
        FROM heats h
        JOIN heat_entries he ON he.heat_id = h.id
        WHERE h.tournament_id = ? AND h.stage = ?
        """,
        (tournament_id, stage),
    ).fetchone()
    return bool(row["total"]) and row["finished"] == row["total"]


def stage_has_results(connection: sqlite3.Connection, tournament_id: int, stage: str) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM heat_entries he
        JOIN heats h ON h.id = he.heat_id
        WHERE h.tournament_id = ? AND h.stage = ? AND he.finish IS NOT NULL
        LIMIT 1
        """,
        (tournament_id, stage),
    ).fetchone()
    return row is not None


def is_staging_heat_locked(connection: sqlite3.Connection, tournament_id: int, global_number: int) -> bool:
    """A staging heat is locked while any earlier staging heat (by global_number,
    the tournament's canonical heat order) still has unscored entries."""
    row = connection.execute(
        """
        SELECT 1
        FROM heats h
        WHERE h.tournament_id = ? AND h.stage = 'staging' AND h.global_number < ?
          AND EXISTS (SELECT 1 FROM heat_entries he WHERE he.heat_id = h.id AND he.finish IS NULL)
        LIMIT 1
        """,
        (tournament_id, global_number),
    ).fetchone()
    return row is not None


def stage_racer_marbles(
    connection: sqlite3.Connection, tournament_id: int, stage: str
) -> list[tuple[int, int, int | None, int | None]] | None:
    """Sorted (racer_id, marble_count, origin_round, origin_heat_id) tuples
    currently in a stage's heats, or None if the stage has no heats at all
    (distinct from "heats exist but are empty"). Marble counts and origin
    metadata, not just racer identity, must match the prospective field -- a
    racer who keeps qualifying but earns a different number of marbles, or
    the same slot from a different round/heat, still needs the stage
    rebuilt so its origin metadata doesn't go stale."""
    heats = connection.execute(
        "SELECT id FROM heats WHERE tournament_id = ? AND stage = ?", (tournament_id, stage)
    ).fetchall()
    if not heats:
        return None
    placeholders = ",".join("?" * len(heats))
    rows = connection.execute(
        f"""
        SELECT racer_id, COUNT(*) AS marble_count,
               MIN(origin_round) AS origin_round, MIN(origin_heat_id) AS origin_heat_id
        FROM heat_entries
        WHERE heat_id IN ({placeholders})
        GROUP BY racer_id
        """,
        [row["id"] for row in heats],
    ).fetchall()
    return sorted(
        (row["racer_id"], row["marble_count"], row["origin_round"], row["origin_heat_id"]) for row in rows
    )


def heat_top_n(connection: sqlite3.Connection, heat_id: int, n: int) -> list[int]:
    """Top n racer ids in a completed heat, ranked by aggregate points (across
    that racer's marbles in the heat), tie-broken by best individual finish."""
    rows = connection.execute(
        """
        SELECT he.racer_id AS racer_id,
               COALESCE(SUM(he.points), 0) AS points,
               MIN(CASE WHEN he.finish > 0 THEN he.finish END) AS best_finish
        FROM heat_entries he
        WHERE he.heat_id = ?
        GROUP BY he.racer_id
        ORDER BY points DESC, best_finish IS NULL, best_finish ASC, racer_id ASC
        """,
        (heat_id,),
    ).fetchall()
    return [row["racer_id"] for row in rows[:n]]


def championship_field(connection: sqlite3.Connection, tournament_id: int) -> dict[str, list[dict[str, Any]]]:
    """Pure computation over every staging round's round_standings().

    Tier priority is bye > preliminary > wildcard: a racer who ever wins a
    round (rank 1) is permanently ineligible for preliminary or wildcard,
    even in rounds where they didn't win. Each round's rank-1 finisher is
    always its bye candidate -- no cascading, no reassignment; wins beyond
    max_bye_marbles_per_racer are simply forfeited (earliest wins kept, by
    day ascending).

    A round's preliminary candidate is normally its rank-2 finisher, but if
    that racer is already bye-ineligible, we cascade down the round's own
    standings (rank 3, 4, ...) until we reach someone who isn't. The
    wildcard pool then draws two seats per round from whoever's left after
    the bye and preliminary claims.

    In the shared-pool model every racer competes in every round, so the
    same racer can qualify for the same tier in more than one round. Rather
    than keeping only their single best slot, each racer's qualifying
    occurrences (by day ascending) become separate marbles in that tier's
    heat. max_bye_marbles_per_racer caps this for the bye and preliminary
    tiers only -- wildcard marbles are uncapped, since a round's two
    wildcard seats are filled fresh from whoever's left every time (unlike
    bye/preliminary, a racer's wildcard occurrence in one round is never
    "spent" in a way that would leave another round's seat empty).
    """
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    marble_cap = tournament["max_bye_marbles_per_racer"]

    rankings: dict[int, list[dict[str, Any]]] = {}
    bye_wins: dict[int, list[int]] = {}
    for day in range(1, tournament["days"] + 1):
        ranking = round_standings(connection, tournament_id, day)
        rankings[day] = ranking
        if ranking:
            bye_wins.setdefault(ranking[0]["id"], []).append(day)

    byes: list[dict[str, Any]] = []
    for racer_id, days_won in bye_wins.items():
        for day in sorted(days_won)[:marble_cap]:
            byes.append({"racerId": racer_id, "originRound": day})
    bye_racer_ids = set(bye_wins.keys())

    # Each round's preliminary occurrence is the first (highest-ranked)
    # finisher who isn't bye-ineligible -- normally rank 2, but cascading
    # past bye-tier racers as far down the standings as it takes.
    prelim_occurrences: dict[int, list[tuple[int, int]]] = {}
    for day, ranking in rankings.items():
        for rank_row in ranking[1:]:
            if rank_row["id"] in bye_racer_ids:
                continue
            prelim_occurrences.setdefault(rank_row["id"], []).append(
                (day, rank_row["totalPoints"])
            )
            break

    preliminary_direct: list[dict[str, Any]] = []
    for racer_id, occurrences in prelim_occurrences.items():
        for day, points in sorted(occurrences)[:marble_cap]:
            preliminary_direct.append({"racerId": racer_id, "originRound": day, "points": points})
    preliminary_racer_ids = set(prelim_occurrences.keys())

    # Each round's wildcard seats are the first two finishers, in rank
    # order, who aren't themselves bye- or preliminary-tier -- uncapped, so
    # a racer eligible in several rounds races a marble for each one.
    wildcard_pool: list[dict[str, Any]] = []
    for day, ranking in rankings.items():
        eligible = [
            rank_row
            for rank_row in ranking[1:]
            if rank_row["id"] not in bye_racer_ids and rank_row["id"] not in preliminary_racer_ids
        ]
        for rank_row in eligible[:2]:
            wildcard_pool.append(
                {"racerId": rank_row["id"], "originRound": day, "points": rank_row["totalPoints"]}
            )

    return {"byes": byes, "preliminaryDirect": preliminary_direct, "wildcardPool": wildcard_pool}


def interleave_groups(
    entries: Sequence[dict[str, Any]],
    group_count: int,
    seed: int,
    bucket_key: Callable[[dict[str, Any]], Any],
) -> list[list[dict[str, Any]]]:
    """Deal entries into group_count heats round-robin by origin bucket, so
    entries sharing a bucket (same staging round, or same prior heat) land in
    different heats whenever that's mathematically possible. This is a one-shot,
    single-appearance dealing problem -- deterministic given seed, not the
    randomized-restart repeated-pairing search balanced_day_schedule performs
    for the (different) multi-appearance staging schedule.
    """
    if group_count <= 0 or not entries:
        return []
    buckets: dict[Any, list[dict[str, Any]]] = {}
    for entry in entries:
        buckets.setdefault(bucket_key(entry), []).append(entry)
    rng = random.Random(seed)
    for bucket in buckets.values():
        rng.shuffle(bucket)
    ordered_buckets = sorted(buckets.values(), key=len, reverse=True)

    heats: list[list[dict[str, Any]]] = [[] for _ in range(group_count)]
    cursor = 0
    remaining = sum(len(bucket) for bucket in ordered_buckets)
    while remaining:
        progressed = False
        for bucket in ordered_buckets:
            if not bucket:
                continue
            heats[cursor % group_count].append(bucket.pop(0))
            cursor += 1
            remaining -= 1
            progressed = True
        if not progressed:
            break

    for _ in range(group_count * group_count):
        heats.sort(key=len)
        smallest, largest = heats[0], heats[-1]
        if len(largest) - len(smallest) <= 1:
            break
        smallest_keys = {bucket_key(entry) for entry in smallest}
        candidate = next(
            (entry for entry in largest if bucket_key(entry) not in smallest_keys),
            largest[0],
        )
        largest.remove(candidate)
        smallest.append(candidate)
    return heats


def _next_global_number(connection: sqlite3.Connection, tournament_id: int) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(global_number), 0) AS max_number FROM heats WHERE tournament_id = ?",
        (tournament_id,),
    ).fetchone()
    return row["max_number"]


def _insert_championship_heat(
    connection: sqlite3.Connection,
    tournament_id: int,
    stage: str,
    heat_number: int,
    global_number: int,
    participants: Sequence[dict[str, Any]],
    default_marbles_per_racer: int = 1,
) -> int:
    """Each participant races default_marbles_per_racer marbles, unless it
    carries its own marbleSlots count (wildcard/preliminary entrants who
    qualified more than once race one marble per qualifying occurrence).
    Each marble is tagged with its own origin (from marbleOrigins, when
    present) rather than the entry's single representative origin, since a
    racer's marbles can trace back to different staging rounds.
    """
    cursor = connection.execute(
        """
        INSERT INTO heats (tournament_id, stage, day, heat_number, global_number)
        VALUES (?, ?, NULL, ?, ?)
        """,
        (tournament_id, stage, heat_number, global_number),
    )
    heat_id = int(cursor.lastrowid)
    rows = []
    for lane, entry in enumerate(participants, start=1):
        marble_count = entry.get("marbleSlots", default_marbles_per_racer)
        origins = entry.get("marbleOrigins")
        for marble_number in range(1, marble_count + 1):
            origin = origins[marble_number - 1] if origins else entry
            rows.append(
                (
                    heat_id,
                    lane,
                    entry["racerId"],
                    marble_number,
                    origin.get("originStage"),
                    origin.get("originRound"),
                    origin.get("originHeatId"),
                )
            )
    connection.executemany(
        """
        INSERT INTO heat_entries
            (heat_id, lane, racer_id, marble_number, origin_stage, origin_round, origin_heat_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return heat_id


def consolidate_by_racer(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge multiple per-occurrence qualifying entries for the same racer
    into a single participant record carrying a marbleSlots count, so a
    racer who qualified more than once races every earned marble in one
    heat instead of being split across heats or colliding on marble_number.

    Each occurrence's own origin metadata is kept, per marble, in
    marbleOrigins (in origin-round order) -- a racer's marbles can come from
    different staging rounds, so this is what lets a heat entry later report
    exactly which round(s) seeded it, not just its earliest one. The
    earliest occurrence's metadata also represents the entry itself, for
    callers that only need a single origin (bucketing, cap comparisons).
    """
    grouped: dict[int, list[dict[str, Any]]] = {}
    order: list[int] = []
    for entry in entries:
        racer_id = entry["racerId"]
        if racer_id not in grouped:
            order.append(racer_id)
        grouped.setdefault(racer_id, []).append(entry)
    consolidated = []
    for racer_id in order:
        occurrences = sorted(grouped[racer_id], key=lambda item: item.get("originRound") or 0)
        primary = occurrences[0]
        merged = dict(primary)
        merged["marbleSlots"] = len(occurrences)
        merged["marbleOrigins"] = [
            {
                "originStage": item.get("originStage"),
                "originRound": item.get("originRound"),
                "originHeatId": item.get("originHeatId"),
            }
            for item in occurrences
        ]
        consolidated.append(merged)
    return consolidated


def wildcard_field(connection: sqlite3.Connection, tournament_id: int) -> list[dict[str, Any]]:
    field = championship_field(connection, tournament_id)
    entries = [
        {"racerId": item["racerId"], "originStage": "staging-round", "originRound": item["originRound"]}
        for item in field["wildcardPool"]
    ]
    return consolidate_by_racer(entries)


def preliminary_field(connection: sqlite3.Connection, tournament_id: int) -> list[dict[str, Any]]:
    field = championship_field(connection, tournament_id)
    entries = [
        {
            "racerId": item["racerId"],
            "originStage": "staging-round",
            "originRound": item["originRound"],
            "bucketKey": ("round", item["originRound"]),
        }
        for item in field["preliminaryDirect"]
    ]
    wildcard_heats = connection.execute(
        "SELECT id FROM heats WHERE tournament_id = ? AND stage = 'wildcard' ORDER BY heat_number",
        (tournament_id,),
    ).fetchall()
    if wildcard_heats:
        for heat_row in wildcard_heats:
            for racer_id in heat_top_n(connection, heat_row["id"], 1):
                entries.append(
                    {
                        "racerId": racer_id,
                        "originStage": "wildcard",
                        "originHeatId": heat_row["id"],
                        "bucketKey": ("heat", heat_row["id"]),
                    }
                )
    elif field["wildcardPool"]:
        for item in field["wildcardPool"]:
            entries.append(
                {
                    "racerId": item["racerId"],
                    "originStage": "stage-skip",
                    "originRound": item["originRound"],
                    "bucketKey": ("round", item["originRound"]),
                }
            )
    return consolidate_by_racer(entries)


def final_field(
    connection: sqlite3.Connection, tournament_id: int
) -> tuple[list[dict[str, Any]], int]:
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    field = championship_field(connection, tournament_id)
    candidates: list[dict[str, Any]] = [
        {"racerId": item["racerId"], "originStage": "bye", "originRound": item["originRound"]}
        for item in field["byes"]
    ]
    preliminary_heats = connection.execute(
        "SELECT id FROM heats WHERE tournament_id = ? AND stage = 'preliminary' ORDER BY heat_number",
        (tournament_id,),
    ).fetchall()
    if preliminary_heats:
        for heat_row in preliminary_heats:
            for racer_id in heat_top_n(connection, heat_row["id"], 2):
                candidates.append(
                    {"racerId": racer_id, "originStage": "preliminary", "originHeatId": heat_row["id"]}
                )
    else:
        for entry in preliminary_field(connection, tournament_id):
            skipped = {
                key: value
                for key, value in entry.items()
                if key not in ("bucketKey", "marbleSlots", "marbleOrigins")
            }
            skipped["originStage"] = "stage-skip"
            candidates.append(skipped)

    seen: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["racerId"] in seen:
            continue
        seen.add(candidate["racerId"])
        deduped.append(candidate)

    max_final = tournament["max_final_racers"]
    trimmed = 0
    if len(deduped) > max_final:
        overall = {row["id"]: row for row in standings(connection, tournament_id)}
        deduped.sort(
            key=lambda candidate: overall.get(candidate["racerId"], {}).get("rank", 10 ** 6)
        )
        trimmed = len(deduped) - max_final
        deduped = deduped[:max_final]
    return deduped, trimmed


def _wildcard_groups_with_marble_splitting(
    entries: Sequence[dict[str, Any]], heat_count: int, seed: int
) -> list[list[dict[str, Any]]]:
    """Group wildcard entries into heat_count heats, splitting a racer's
    marbleSlots evenly across multiple heats when they have more marbles
    than a single heat should absorb -- instead of crowding out other
    racers by stacking every marble from one racer into one heat.

    interleave_groups() first decides each racer's "anchor" heat the usual
    way (spread by origin round). A racer with only one marble stays there.
    A racer with more marbles fans out chunk_count = min(marbleSlots,
    heat_count) chunks starting at their anchor and stepping to the next
    heat index each time (wrapping around), which -- because chunk_count
    never exceeds heat_count -- guarantees no two chunks from the same
    racer ever land in the same heat (that would collide on marble_number).
    """
    if heat_count <= 1:
        return interleave_groups(entries, heat_count, seed, lambda entry: entry["originRound"])
    anchor_groups = interleave_groups(entries, heat_count, seed, lambda entry: entry["originRound"])
    anchor_index = {
        entry["racerId"]: index for index, group in enumerate(anchor_groups) for entry in group
    }
    groups: list[list[dict[str, Any]]] = [[] for _ in range(heat_count)]
    for entry in entries:
        marbles = entry["marbleSlots"]
        anchor = anchor_index[entry["racerId"]]
        if marbles <= 1:
            groups[anchor].append(entry)
            continue
        chunk_count = min(marbles, heat_count)
        base, extra = divmod(marbles, chunk_count)
        origins = entry.get("marbleOrigins")
        cursor = 0
        for offset in range(chunk_count):
            heat_index = (anchor + offset) % heat_count
            chunk_size = base + (1 if offset < extra else 0)
            chunk = dict(entry)
            chunk["marbleSlots"] = chunk_size
            if origins is not None:
                chunk["marbleOrigins"] = origins[cursor : cursor + chunk_size]
                cursor += chunk_size
            groups[heat_index].append(chunk)
    return groups


def build_wildcard_heats(connection: sqlite3.Connection, tournament_id: int) -> None:
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    entries = wildcard_field(connection, tournament_id)
    if not entries:
        return
    # Entries carry their own marbleSlots (racers who qualified more than
    # once race more than one marble), so heat sizing is budgeted against
    # total marble volume rather than racer count.
    total_marbles = sum(entry["marbleSlots"] for entry in entries)
    _marbles_per_heat, heat_count = calculate_championship_heat_size(
        total_marbles, tournament["championship_max_marbles_per_heat"], 1
    )
    if heat_count == 0:
        return
    global_number = _next_global_number(connection, tournament_id)
    groups = [
        group
        for group in _wildcard_groups_with_marble_splitting(entries, heat_count, tournament["seed"])
        if group
    ]
    for heat_number, group in enumerate(groups, start=1):
        global_number += 1
        _insert_championship_heat(connection, tournament_id, "wildcard", heat_number, global_number, group)


def build_preliminary_heats(connection: sqlite3.Connection, tournament_id: int) -> None:
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    entries = preliminary_field(connection, tournament_id)
    if not entries:
        return
    total_marbles = sum(entry["marbleSlots"] for entry in entries)
    _marbles_per_heat, heat_count = calculate_championship_heat_size(
        total_marbles, tournament["championship_max_marbles_per_heat"], 1
    )
    if heat_count == 0:
        return
    global_number = _next_global_number(connection, tournament_id)
    groups = interleave_groups(entries, heat_count, tournament["seed"] + 101, lambda entry: entry["bucketKey"])
    for heat_number, group in enumerate(groups, start=1):
        global_number += 1
        _insert_championship_heat(connection, tournament_id, "preliminary", heat_number, global_number, group)


def build_final_heat(connection: sqlite3.Connection, tournament_id: int) -> None:
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    candidates, _trimmed = final_field(connection, tournament_id)
    if not candidates:
        return
    global_number = _next_global_number(connection, tournament_id)
    ordered = list(candidates)
    random.Random(tournament["seed"] + 202).shuffle(ordered)
    # The final always races one marble per racer, regardless of the
    # tournament's marbles_per_racer setting -- champion/podium/DNF logic
    # (both server and frontend) all key off a single finish per racer, which
    # only holds when each finalist has exactly one marble.
    _insert_championship_heat(connection, tournament_id, "final", 1, global_number + 1, ordered, 1)


def delete_championship_stages(connection: sqlite3.Connection, tournament_id: int, from_stage: str) -> None:
    stages = STAGE_CASCADE[from_stage]
    placeholders = ",".join("?" for _ in stages)
    connection.execute(
        f"DELETE FROM heats WHERE tournament_id = ? AND stage IN ({placeholders})",
        (tournament_id, *stages),
    )


def _stage_ready_to_advance(
    connection: sqlite3.Connection,
    tournament_id: int,
    stage: str,
    field_size: int,
    tournament_row: sqlite3.Row,
) -> bool:
    """True once this stage no longer blocks the next one: either its field is
    empty/too small to form a heat (auto-advance -- nothing to wait for) or its
    heats exist and are fully scored. Deciding this from field_size (total
    marbles, the same sizing calculation build_*_heats uses) + config rather
    than from "did a heat row get created" is what lets the skip case be
    recognized as settled even though it leaves zero rows behind.
    """
    if field_size == 0:
        return True
    _marbles_per_heat, heat_count = calculate_championship_heat_size(
        field_size, tournament_row["championship_max_marbles_per_heat"], 1
    )
    if heat_count == 0:
        return True
    return is_stage_complete(connection, tournament_id, stage)


def _field_marble_signature(
    entries: Sequence[dict[str, Any]], marble_slots_key: str | None = None
) -> list[tuple[int, int, int | None, int | None]]:
    """Sorted (racer_id, marble_count, origin_round, origin_heat_id) tuples
    for a prospective field, in the same shape stage_racer_marbles() returns
    for what's currently stored -- so a change in marble count or origin,
    not just racer identity, is detected as a field change."""
    return sorted(
        (
            item["racerId"],
            item[marble_slots_key] if marble_slots_key else 1,
            item.get("originRound"),
            item.get("originHeatId"),
        )
        for item in entries
    )


def sync_championship(connection: sqlite3.Connection, tournament_id: int) -> None:
    """Recompute-and-compare pipeline: for each stage in order, compare its
    prospective racer field to what's currently stored, rebuilding (and wiping
    everything downstream) on a mismatch. Then, whether or not a rebuild just
    happened, check whether the stage is settled -- either its field was too
    small to race (skip straight through) or its heats are fully scored -- and
    only proceed to the next stage once it is.
    """
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    staging_total = tournament["days"] * tournament["heats_per_day"]
    if staging_total == 0 or completed_heat_count(connection, tournament_id, stage="staging") != staging_total:
        delete_championship_stages(connection, tournament_id, "wildcard")
        return

    wildcard_entries = wildcard_field(connection, tournament_id)
    wildcard_marbles = _field_marble_signature(wildcard_entries, "marbleSlots")
    current_wildcard_marbles = stage_racer_marbles(connection, tournament_id, "wildcard")
    if wildcard_marbles != (current_wildcard_marbles or []):
        delete_championship_stages(connection, tournament_id, "wildcard")
        if wildcard_marbles:
            build_wildcard_heats(connection, tournament_id)
    total_wildcard_marbles = sum(entry["marbleSlots"] for entry in wildcard_entries)
    if not _stage_ready_to_advance(connection, tournament_id, "wildcard", total_wildcard_marbles, tournament):
        delete_championship_stages(connection, tournament_id, "preliminary")
        return

    preliminary_entries = preliminary_field(connection, tournament_id)
    preliminary_marbles = _field_marble_signature(preliminary_entries, "marbleSlots")
    current_preliminary_marbles = stage_racer_marbles(connection, tournament_id, "preliminary")
    if preliminary_marbles != (current_preliminary_marbles or []):
        delete_championship_stages(connection, tournament_id, "preliminary")
        if preliminary_marbles:
            build_preliminary_heats(connection, tournament_id)
    total_preliminary_marbles = sum(entry["marbleSlots"] for entry in preliminary_entries)
    if not _stage_ready_to_advance(connection, tournament_id, "preliminary", total_preliminary_marbles, tournament):
        delete_championship_stages(connection, tournament_id, "final")
        return

    final_candidates, _trimmed = final_field(connection, tournament_id)
    final_ids = _field_marble_signature(final_candidates)
    current_final_ids = stage_racer_marbles(connection, tournament_id, "final")
    if final_ids != (current_final_ids or []):
        delete_championship_stages(connection, tournament_id, "final")
        if final_ids:
            build_final_heat(connection, tournament_id)


def stages_pending_cascade_reset(connection: sqlite3.Connection, tournament_id: int) -> list[str]:
    """Which championship stages currently hold entered results but would be
    rebuilt (their field changed) if sync_championship() ran right now. Used to
    gate a heat-result save behind confirmReset before it silently wipes
    already-scored downstream work.
    """
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    staging_total = tournament["days"] * tournament["heats_per_day"]
    staging_complete = (
        staging_total > 0 and completed_heat_count(connection, tournament_id, stage="staging") == staging_total
    )
    if not staging_complete:
        return [stage for stage in CHAMPIONSHIP_STAGES if stage_has_results(connection, tournament_id, stage)]

    wildcard_entries = wildcard_field(connection, tournament_id)
    wildcard_marbles = _field_marble_signature(wildcard_entries, "marbleSlots")
    current_wildcard_marbles = stage_racer_marbles(connection, tournament_id, "wildcard")
    if wildcard_marbles != (current_wildcard_marbles or []):
        return [stage for stage in CHAMPIONSHIP_STAGES if stage_has_results(connection, tournament_id, stage)]
    total_wildcard_marbles = sum(entry["marbleSlots"] for entry in wildcard_entries)
    if not _stage_ready_to_advance(connection, tournament_id, "wildcard", total_wildcard_marbles, tournament):
        return []

    preliminary_entries = preliminary_field(connection, tournament_id)
    preliminary_marbles = _field_marble_signature(preliminary_entries, "marbleSlots")
    current_preliminary_marbles = stage_racer_marbles(connection, tournament_id, "preliminary")
    if preliminary_marbles != (current_preliminary_marbles or []):
        return [
            stage
            for stage in ("preliminary", "final")
            if stage_has_results(connection, tournament_id, stage)
        ]
    total_preliminary_marbles = sum(entry["marbleSlots"] for entry in preliminary_entries)
    if not _stage_ready_to_advance(connection, tournament_id, "preliminary", total_preliminary_marbles, tournament):
        return []

    final_candidates, _trimmed = final_field(connection, tournament_id)
    final_ids = _field_marble_signature(final_candidates)
    current_final_ids = stage_racer_marbles(connection, tournament_id, "final")
    if final_ids != (current_final_ids or []) and stage_has_results(connection, tournament_id, "final"):
        return ["final"]
    return []
