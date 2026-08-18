from __future__ import annotations

import os
import random
import sqlite3
from collections import Counter
from contextlib import contextmanager
from itertools import combinations
from math import gcd
from pathlib import Path
from typing import Any, Iterator, Sequence


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
        final_racers INTEGER NOT NULL CHECK (final_racers >= 2),
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
        day INTEGER NOT NULL,
        heat_number INTEGER NOT NULL,
        global_number INTEGER NOT NULL,
        UNIQUE (tournament_id, day, heat_number),
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
        PRIMARY KEY (heat_id, lane, marble_number),
        UNIQUE (heat_id, racer_id, marble_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS final_entries (
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        seed INTEGER NOT NULL,
        racer_id INTEGER NOT NULL REFERENCES racers(id) ON DELETE CASCADE,
        finish INTEGER,
        PRIMARY KEY (tournament_id, seed),
        UNIQUE (tournament_id, racer_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_heats_day ON heats (tournament_id, day, heat_number)",
    "CREATE INDEX IF NOT EXISTS idx_heat_entries_racer ON heat_entries (racer_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tournaments_name_nocase ON tournaments (name COLLATE NOCASE)",
]


def create_tournament(connection: sqlite3.Connection, name: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO tournaments
            (name, days, heats_per_day, heats_per_racer_per_day,
             racers_per_heat, max_marbles_per_heat, marbles_per_racer,
             final_racers, seed)
        VALUES (?, 3, 4, 3, 6, 6, 1, 6, 7)
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
    connection.execute(
        "DELETE FROM final_entries WHERE tournament_id = ?", (tournament_id,)
    )
    connection.execute("DELETE FROM heats WHERE tournament_id = ?", (tournament_id,))
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
                INSERT INTO heats (tournament_id, day, heat_number, global_number)
                VALUES (?, ?, ?, ?)
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
    tournament = connection.execute(
        "SELECT days FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    rows = connection.execute(
        """
        SELECT r.id, r.name, r.color, r.sort_order,
               COALESCE(SUM(he.points), 0) AS total_points,
               COALESCE(SUM(CASE WHEN he.finish = 1 THEN 1 ELSE 0 END), 0) AS wins
        FROM racers r
        LEFT JOIN heat_entries he ON he.racer_id = r.id
        WHERE r.tournament_id = ?
        GROUP BY r.id
        ORDER BY total_points DESC, wins DESC, r.sort_order ASC
        """,
        (tournament_id,),
    ).fetchall()
    day_rows = connection.execute(
        """
        SELECT he.racer_id, h.day, COALESCE(SUM(he.points), 0) AS points
        FROM heat_entries he
        JOIN heats h ON h.id = he.heat_id
        WHERE h.tournament_id = ?
        GROUP BY he.racer_id, h.day
        """,
        (tournament_id,),
    ).fetchall()
    day_points = {(row["racer_id"], row["day"]): row["points"] for row in day_rows}
    result = []
    for rank, row in enumerate(rows, start=1):
        result.append(
            {
                "rank": rank,
                "id": row["id"],
                "name": row["name"],
                "color": row["color"],
                "totalPoints": row["total_points"],
                "wins": row["wins"],
                "dayPoints": [day_points.get((row["id"], day), 0) for day in range(1, tournament["days"] + 1)],
            }
        )
    return result


def completed_heat_count(connection: sqlite3.Connection, tournament_id: int) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS completed
        FROM (
            SELECT h.id
            FROM heats h
            JOIN heat_entries he ON he.heat_id = h.id
            WHERE h.tournament_id = ?
            GROUP BY h.id
            HAVING COUNT(*) = SUM(CASE WHEN he.finish IS NOT NULL THEN 1 ELSE 0 END)
        )
        """,
        (tournament_id,),
    ).fetchone()
    return row["completed"]


def sync_final(connection: sqlite3.Connection, tournament_id: int) -> None:
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    total_heats = tournament["days"] * tournament["heats_per_day"]
    if completed_heat_count(connection, tournament_id) != total_heats:
        connection.execute(
            "DELETE FROM final_entries WHERE tournament_id = ?", (tournament_id,)
        )
        return
    qualifiers = standings(connection, tournament_id)[: tournament["final_racers"]]
    connection.execute(
        "DELETE FROM final_entries WHERE tournament_id = ?", (tournament_id,)
    )
    connection.executemany(
        """
        INSERT INTO final_entries (tournament_id, seed, racer_id)
        VALUES (?, ?, ?)
        """,
        [(tournament_id, item["rank"], item["id"]) for item in qualifiers],
    )
