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

STAGE_CASCADE = {
    "wildcard": ("wildcard", "preliminary", "quarterfinal", "semifinal", "final"),
    "preliminary": ("preliminary", "quarterfinal", "semifinal", "final"),
    "quarterfinal": ("quarterfinal", "semifinal", "final"),
    "semifinal": ("semifinal", "final"),
    "final": ("final",),
}


def database_path() -> Path:
    data_dir = Path(os.environ.get("APP_DATA_DIR", Path(__file__).parent.parent / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "rollrank.db"


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
        rounds INTEGER NOT NULL CHECK (rounds >= 1),
        heats_per_round INTEGER NOT NULL CHECK (heats_per_round >= 1),
        heats_per_racer_per_round INTEGER NOT NULL DEFAULT 3 CHECK (heats_per_racer_per_round >= 1),
        racers_per_heat INTEGER NOT NULL CHECK (racers_per_heat >= 2),
        max_marbles_per_heat INTEGER NOT NULL CHECK (max_marbles_per_heat >= 2),
        marbles_per_racer INTEGER NOT NULL DEFAULT 1 CHECK (marbles_per_racer >= 1),
        wildcard_max_marbles_per_heat INTEGER NOT NULL DEFAULT 6 CHECK (wildcard_max_marbles_per_heat >= 2),
        preliminary_max_marbles_per_heat INTEGER NOT NULL DEFAULT 6 CHECK (preliminary_max_marbles_per_heat >= 2),
        max_final_bye_marbles_per_racer INTEGER NOT NULL DEFAULT 2 CHECK (max_final_bye_marbles_per_racer BETWEEN 0 AND 20),
        max_prelim_marbles_for_racer_with_final_bye INTEGER NOT NULL DEFAULT 0 CHECK (max_prelim_marbles_for_racer_with_final_bye BETWEEN 0 AND 20),
        max_wildcard_marbles_for_racer_with_final_bye INTEGER NOT NULL DEFAULT 0 CHECK (max_wildcard_marbles_for_racer_with_final_bye BETWEEN 0 AND 20),
        allow_cascading_final_bye_selection INTEGER NOT NULL DEFAULT 1 CHECK (allow_cascading_final_bye_selection IN (0, 1)),
        max_prelim_promotion_marbles_per_racer INTEGER NOT NULL DEFAULT 1 CHECK (max_prelim_promotion_marbles_per_racer BETWEEN 0 AND 20),
        allow_cascading_prelim_promotion_selection INTEGER NOT NULL DEFAULT 1 CHECK (allow_cascading_prelim_promotion_selection IN (0, 1)),
        max_wildcard_marbles_for_racer_with_prelim_promotion INTEGER NOT NULL DEFAULT 0 CHECK (max_wildcard_marbles_for_racer_with_prelim_promotion BETWEEN 0 AND 20),
        max_wildcard_promotion_marbles_per_racer INTEGER NOT NULL DEFAULT 2 CHECK (max_wildcard_promotion_marbles_per_racer BETWEEN 0 AND 20),
        allow_cascading_wildcard_promotion_selection INTEGER NOT NULL DEFAULT 1 CHECK (allow_cascading_wildcard_promotion_selection IN (0, 1)),
        final_racers_promoted_per_round INTEGER NOT NULL DEFAULT 1 CHECK (final_racers_promoted_per_round BETWEEN 1 AND 24),
        preliminary_racers_promoted_per_round INTEGER NOT NULL DEFAULT 1 CHECK (preliminary_racers_promoted_per_round BETWEEN 1 AND 24),
        wildcard_racers_promoted_per_round INTEGER NOT NULL DEFAULT 2 CHECK (wildcard_racers_promoted_per_round BETWEEN 1 AND 24),
        wildcard_racers_promoted_per_heat INTEGER NOT NULL DEFAULT 2 CHECK (wildcard_racers_promoted_per_heat BETWEEN 1 AND 24),
        preliminary_racers_promoted_per_heat INTEGER NOT NULL DEFAULT 2 CHECK (preliminary_racers_promoted_per_heat BETWEEN 1 AND 24),
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
        stage TEXT NOT NULL DEFAULT 'staging' CHECK (stage IN ('staging','wildcard','preliminary','quarterfinal','semifinal','final')),
        round INTEGER,
        heat_number INTEGER NOT NULL,
        global_number INTEGER NOT NULL,
        started_at TEXT,
        UNIQUE (tournament_id, stage, round, heat_number),
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
    """
    CREATE TABLE IF NOT EXISTS round_tiebreaks (
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        round INTEGER NOT NULL,
        racer_id INTEGER NOT NULL REFERENCES racers(id) ON DELETE CASCADE,
        resolved_rank INTEGER NOT NULL,
        resolved_at TEXT,
        PRIMARY KEY (tournament_id, round, racer_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_heats_round ON heats (tournament_id, round, heat_number)",
    "CREATE INDEX IF NOT EXISTS idx_heats_stage ON heats (tournament_id, stage)",
    "CREATE INDEX IF NOT EXISTS idx_heat_entries_racer ON heat_entries (racer_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tournaments_name_nocase ON tournaments (name COLLATE NOCASE)",
]


def create_tournament(connection: sqlite3.Connection, name: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO tournaments
            (name, rounds, heats_per_round, heats_per_racer_per_round,
             racers_per_heat, max_marbles_per_heat, marbles_per_racer,
             wildcard_max_marbles_per_heat, preliminary_max_marbles_per_heat,
             max_final_bye_marbles_per_racer,
             max_prelim_marbles_for_racer_with_final_bye,
             max_wildcard_marbles_for_racer_with_final_bye,
             allow_cascading_final_bye_selection,
             max_prelim_promotion_marbles_per_racer,
             allow_cascading_prelim_promotion_selection,
             max_wildcard_marbles_for_racer_with_prelim_promotion,
             max_wildcard_promotion_marbles_per_racer,
             allow_cascading_wildcard_promotion_selection,
             wildcard_racers_promoted_per_heat, preliminary_racers_promoted_per_heat,
             max_final_racers, seed)
        VALUES (?, 3, 4, 3, 6, 6, 1, 6, 6, 2, 0, 0, 1, 1, 1, 0, 2, 1, 2, 2, 6, 7)
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
    """The heat count a championship field splits into, filling heats as
    close to max_marbles_per_heat as the marble limit allows and spreading
    any remainder as evenly as possible across heats -- unlike a staging
    round's repeated heats, a one-off championship promotion heat doesn't
    need every heat to come out exactly the same size, so a field that
    doesn't divide evenly (7 racers at a max of 4, say) still splits into
    heats instead of forcing one oversized heat or skipping the stage
    entirely. interleave_groups() (used by callers to actually place
    entries) already tolerates -- and rebalances toward -- heats within one
    racer of each other, so it fills in the rest correctly once this
    returns a sane heat_count.

    Returns (0, 0) when no heat of at least 2 racers can be formed at all
    (field too small, or the marble limit is too tight to seat even a
    pair) -- callers treat that as "the whole field auto-advances to the
    next stage without racing" rather than raising.
    """
    if field_size < 2:
        return 0, 0
    max_racers_per_heat = min(MAX_RACERS_PER_HEAT, max_marbles_per_heat // marbles_per_racer)
    if max_racers_per_heat < 2:
        return 0, 0
    heat_count = -(-field_size // max_racers_per_heat)  # ceiling division
    # Never ask for more heats than the field can fill with at least 2
    # racers each -- a heat of 1 can't race against anyone.
    heat_count = min(heat_count, field_size // 2)
    return field_size // heat_count, heat_count


def final_bracket_stage(candidate_count: int, max_final_racers: int) -> str:
    """'final', 'semifinal', or 'quarterfinal' -- how much bracket splitting
    the seeded-plus-bye final candidate pool needs before it fits
    max_final_racers. Mirrors calculate_championship_heat_size's field_size
    // 2 guard: never split down to a heat with fewer than 2 racers, so an
    unsplittable pool just runs one final heat over cap instead.
    """
    if candidate_count <= max_final_racers or candidate_count < 4:
        return "final"
    semifinal_heat_size = -(-candidate_count // 2)  # ceiling division
    if semifinal_heat_size <= max_final_racers or candidate_count < 8:
        return "semifinal"
    return "quarterfinal"


def balanced_round_schedule(
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
        round_pairs: Counter[tuple[int, int]] = Counter()
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
                        + round_pairs[tuple(sorted((racer_id, opponent)))] * 7
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
                round_pairs[tuple(sorted((first, second)))] += 1
            groups.append(group)

        if not valid or any(remaining.values()):
            continue
        combined = [existing_pair_counts[pair] + round_pairs[pair] for pair in all_pairs]
        daily = [round_pairs[pair] for pair in all_pairs]
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
    rounds: int,
    seed: int,
) -> list[list[list[int]]]:
    pair_counts: Counter[tuple[int, int]] = Counter()
    schedule: list[list[list[int]]] = []
    for round in range(1, rounds + 1):
        round_schedule = balanced_round_schedule(
            racer_ids,
            race_size,
            heats_per_racer,
            pair_counts,
            seed + round * 37,
        )
        schedule.append(round_schedule)
        for group in round_schedule:
            for first, second in combinations(group, 2):
                pair_counts[tuple(sorted((first, second)))] += 1
    return improve_pair_balance(schedule, racer_ids, seed)


def improve_pair_balance(
    schedule: list[list[list[int]]], racer_ids: Sequence[int], seed: int
) -> list[list[list[int]]]:
    """Improve global opponent balance with within-round swaps.

    Swapping racers between two heats on the same round preserves every racer's
    exact daily appearance count and the configured heat size.
    """
    rng = random.Random(seed * 7919 + 17)
    all_pairs = [tuple(sorted(pair)) for pair in combinations(racer_ids, 2)]
    pair_counts: Counter[tuple[int, int]] = Counter()
    for round_schedule in schedule:
        for group in round_schedule:
            pair_counts.update(tuple(sorted(pair)) for pair in combinations(group, 2))

    def objective(counts: Counter[tuple[int, int]]) -> tuple[int, int]:
        values = [counts[pair] for pair in all_pairs]
        return max(values) - min(values), sum(value * value for value in values)

    current_score = objective(pair_counts)
    heat_total = sum(len(round_schedule) for round_schedule in schedule)
    iterations = min(50_000, max(5_000, heat_total * 700))
    for _ in range(iterations):
        if current_score[0] == 0:
            break
        round_schedule = rng.choice(schedule)
        if len(round_schedule) < 2:
            continue
        first_index, second_index = rng.sample(range(len(round_schedule)), 2)
        first_group = round_schedule[first_index]
        second_group = round_schedule[second_index]
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
            round_schedule[first_index] = new_first
            round_schedule[second_index] = new_second
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
    total_slots_per_round = len(racer_ids) * tournament["heats_per_racer_per_round"]
    if total_slots_per_round % tournament["racers_per_heat"]:
        raise ValueError("The racer appearances do not divide into complete heats.")
    heats_per_round = total_slots_per_round // tournament["racers_per_heat"]
    connection.execute(
        "UPDATE tournaments SET heats_per_round = ? WHERE id = ?",
        (heats_per_round, tournament_id),
    )
    schedule = balanced_schedule(
        racer_ids,
        tournament["racers_per_heat"],
        tournament["heats_per_racer_per_round"],
        tournament["rounds"],
        tournament["seed"],
    )
    global_index = 0
    for round, round_schedule in enumerate(schedule, start=1):
        for heat_number, participants in enumerate(round_schedule, start=1):
            global_index += 1
            cursor = connection.execute(
                """
                INSERT INTO heats (tournament_id, stage, round, heat_number, global_number)
                VALUES (?, 'staging', ?, ?, ?)
                """,
                (tournament_id, round, heat_number, global_index),
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
    rank is decided by how many rounds they won, then how many rounds
    promoted them to preliminary, then how many advanced them to wildcard --
    the same bye/preliminary/wildcard tiers the championship field itself
    uses, not raw 2nd/3rd/4th place counts, so cascading (e.g. a bye-
    ineligible 2nd place bumping preliminary down to 3rd) is reflected in
    the ranking too. The sum of their round placings (lower is better) is
    the final tiebreak before falling back to seed order.
    """
    tournament = connection.execute(
        "SELECT rounds FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    racers = connection.execute(
        "SELECT id, name, color, sort_order FROM racers WHERE tournament_id = ? ORDER BY sort_order ASC",
        (tournament_id,),
    ).fetchall()

    field = championship_field(connection, tournament_id)
    round_tier: dict[tuple[int, int], str] = {}
    for item in field["byes"]:
        round_tier[(item["originRound"], item["racerId"])] = "bye"
    for item in field["preliminaryDirect"]:
        round_tier.setdefault((item["originRound"], item["racerId"]), "preliminary")
    for item in field["wildcardPool"]:
        round_tier.setdefault((item["originRound"], item["racerId"]), "wildcard")

    preview = live_round_preview(connection, tournament_id)
    projected_round_tier = preview["roundTiers"] if preview is not None else round_tier

    round_placements: dict[int, list[int | None]] = {row["id"]: [] for row in racers}
    round_display_placements: dict[int, list[int | None]] = {row["id"]: [] for row in racers}
    round_tiers: dict[int, list[str | None]] = {row["id"]: [] for row in racers}
    previous_round_tiers: dict[int, list[str | None]] = {row["id"]: [] for row in racers}
    provisional_round_tiers: dict[int, list[bool]] = {row["id"]: [] for row in racers}
    round_tied_with: dict[int, list[list[dict[str, Any]] | None]] = {row["id"]: [] for row in racers}
    round_tie_collapsed: dict[int, list[bool]] = {row["id"]: [] for row in racers}
    round_tie_resolved: dict[int, list[bool]] = {row["id"]: [] for row in racers}
    round_tie_resolved_at: dict[int, list[str | None]] = {row["id"]: [] for row in racers}
    round_complete_flags: list[bool] = []
    for round in range(1, tournament["rounds"] + 1):
        ranking = round_standings(connection, tournament_id, round)
        round_complete = is_staging_round_complete(connection, tournament_id, round)
        round_complete_flags.append(round_complete)
        is_live_round = preview is not None and preview["round"] == round
        # Racers who match on points and the full placement vector (1st
        # count, 2nd count, ...) for this round are only separated by seed
        # order in round_standings() -- group them here so the standings
        # table can flag exactly which round's placement was a tiebreak
        # rather than a clear result.
        round_tie_groups: dict[tuple[int, tuple[int, ...]], list[dict[str, Any]]] = {}
        for row in ranking:
            round_tie_groups.setdefault(
                (row["totalPoints"], row["placementVector"]), []
            ).append(row)
        round_tiebreak_rows = connection.execute(
            "SELECT racer_id, resolved_rank, resolved_at FROM round_tiebreaks WHERE tournament_id = ? AND round = ?",
            (tournament_id, round),
        ).fetchall()
        existing_round_override = {row["racer_id"]: row["resolved_rank"] for row in round_tiebreak_rows}
        round_resolved_at = next((row["resolved_at"] for row in round_tiebreak_rows), None)
        resolved_racer_ids = set(existing_round_override)
        # A tie that doesn't actually change who gets a bye/preliminary/
        # wildcard seat -- checked cluster by cluster, since one round can hold
        # more than one independent tie and only some of them may matter --
        # never needs an organizer's input. Rather than keep implying a real
        # order via the seed-order fallback, show it as the tie it actually
        # is: a dense ranking where the whole group shares one place and the
        # next distinct racer takes the very next number (two racers tied
        # for 5th are both "5th," and whoever's next is "6th," not "7th").
        # ranking is already sorted, so a group's members are always
        # adjacent.
        display_rank_by_id: dict[int, int] = {}
        collapsed_racer_ids: set[int] = set()
        if round_complete:
            base_snapshot: frozenset[tuple[str, int, int]] | None = None
            next_rank = 1
            index = 0
            while index < len(ranking):
                row = ranking[index]
                key = (row["totalPoints"], row["placementVector"])
                group = round_tie_groups[key]
                group_racer_ids = {other["id"] for other in group}
                is_open_tie = len(group) > 1 and not resolved_racer_ids.issuperset(group_racer_ids)
                if is_open_tie:
                    if base_snapshot is None:
                        base_snapshot = _championship_tier_snapshot(connection, tournament_id)
                    # Pass the group in its actual rank order, not the set
                    # above -- reversing an arbitrary set-iteration order
                    # isn't guaranteed to differ from the current default
                    # order at all, which would silently look identical to
                    # base_snapshot and misreport a real tie as harmless.
                    ordered_racer_ids = [other["id"] for other in group]
                    consequential = _is_tie_group_consequential(
                        connection, tournament_id, round, ordered_racer_ids, existing_round_override, base_snapshot
                    )
                else:
                    consequential = False
                collapsible = is_open_tie and not consequential
                if collapsible:
                    while index < len(ranking) and (
                        ranking[index]["totalPoints"],
                        ranking[index]["placementVector"],
                    ) == key:
                        display_rank_by_id[ranking[index]["id"]] = next_rank
                        collapsed_racer_ids.add(ranking[index]["id"])
                        index += 1
                else:
                    display_rank_by_id[row["id"]] = next_rank
                    index += 1
                next_rank += 1
        for row in ranking:
            if round_complete:
                round_placements[row["id"]].append(row["rank"])
                previous_tier = round_tier.get((round, row["id"]))
                displayed_tier = projected_round_tier.get((round, row["id"]))
            elif is_live_round:
                # Not finalized, but there's a real (partial) result to
                # preview -- including any earlier-round tiers that would
                # be reassigned if the live round ended now. The frontend
                # compares displayed and previous tiers so those cascading
                # changes are visible instead of silently changing history.
                round_placements[row["id"]].append(row["rank"])
                previous_tier = None
                displayed_tier = projected_round_tier.get((round, row["id"]))
            else:
                round_placements[row["id"]].append(None)
                previous_tier = None
                displayed_tier = None
            group = round_tie_groups[(row["totalPoints"], row["placementVector"])]
            group_racer_ids = {other["id"] for other in group}
            group_resolved = len(group) > 1 and resolved_racer_ids.issuperset(group_racer_ids)
            if (round_complete or is_live_round) and len(group) > 1:
                tied_with = [
                    {"id": other["id"], "name": other["name"], "color": other["color"]}
                    for other in group
                    if other["id"] != row["id"]
                ]
            else:
                tied_with = None
            if round_complete:
                display_place = display_rank_by_id[row["id"]]
            else:
                display_place = round_placements[row["id"]][-1]
            round_display_placements[row["id"]].append(display_place)
            round_tie_collapsed[row["id"]].append(row["id"] in collapsed_racer_ids)
            round_tiers[row["id"]].append(displayed_tier)
            previous_round_tiers[row["id"]].append(previous_tier)
            provisional_round_tiers[row["id"]].append(displayed_tier != previous_tier)
            round_tied_with[row["id"]].append(tied_with)
            round_tie_resolved[row["id"]].append(group_resolved)
            round_tie_resolved_at[row["id"]].append(round_resolved_at if group_resolved else None)

    summaries = []
    for row in racers:
        placements = round_placements[row["id"]]
        tiers = round_tiers[row["id"]]
        previous_tiers = previous_round_tiers[row["id"]]
        finalized_placements = [
            place for place, complete in zip(placements, round_complete_flags) if complete
        ]
        summaries.append(
            {
                "id": row["id"],
                "name": row["name"],
                "color": row["color"],
                "sortOrder": row["sort_order"],
                "wins": sum(1 for place in finalized_placements if place == 1),
                "preliminaryPromotions": sum(
                    1
                    for tier, complete in zip(previous_tiers, round_complete_flags)
                    if complete and tier == "preliminary"
                ),
                "wildcardAdvancements": sum(
                    1
                    for tier, complete in zip(previous_tiers, round_complete_flags)
                    if complete and tier == "wildcard"
                ),
                "placementSum": sum(finalized_placements),
                "roundPlacements": placements,
                "roundDisplayPlacements": round_display_placements[row["id"]],
                "roundChampionshipTiers": tiers,
                "roundChampionshipPreviousTiers": previous_tiers,
                "roundChampionshipTierProvisional": provisional_round_tiers[row["id"]],
                "roundTiedWith": round_tied_with[row["id"]],
                "roundTieCollapsed": round_tie_collapsed[row["id"]],
                "roundTieResolved": round_tie_resolved[row["id"]],
                "roundTieResolvedAt": round_tie_resolved_at[row["id"]],
                "liveRoundLeader": preview is not None and preview["leaderId"] == row["id"],
                "liveTier": preview["tiers"].get(row["id"]) if preview is not None else None,
            }
        )

    summaries.sort(
        key=lambda s: (
            -s["wins"],
            -s["preliminaryPromotions"],
            -s["wildcardAdvancements"],
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
    connection: sqlite3.Connection,
    tournament_id: int,
    round: int,
    tie_override: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Same shape/tiebreak as standings(), scoped to a single staging round.

    Points equal, racers are next ranked by how many times they placed 1st,
    then (still equal) how many times they placed 2nd, then 3rd, and so on
    through every placement reached by anyone that round -- a racer with two
    2nd-place finishes outranks one with four 3rd-place finishes even if a
    heat's point spread happens to score those the same. A DNF counts as
    worse than any real placement, so it's only compared once every real
    placement level ties too, with fewer DNFs winning.

    Only once that's tied too does a manual resolution decide it -- an
    organizer's saved answer to "who wins this tie," looked up from
    round_tiebreaks and applied here as a racer_id -> rank mapping (lower
    wins). `tie_override`, when given explicitly, is used in place of that
    lookup instead -- this lets callers test a hypothetical resolution
    without writing it to the database, which is how pending_round_tiebreak()
    determines whether a given tie actually changes anything. Seed order is
    the final fallback for racers this override doesn't mention.
    """
    rows = connection.execute(
        """
        SELECT r.id, r.name, r.color, r.sort_order,
               COALESCE(SUM(he.points), 0) AS total_points
        FROM racers r
        LEFT JOIN heats h ON h.tournament_id = r.tournament_id AND h.stage = 'staging' AND h.round = ?
        LEFT JOIN heat_entries he ON he.heat_id = h.id AND he.racer_id = r.id
        WHERE r.tournament_id = ?
        GROUP BY r.id
        """,
        (round, tournament_id),
    ).fetchall()

    finishes_by_racer: dict[int, list[int]] = {}
    for finish_row in connection.execute(
        """
        SELECT he.racer_id, he.finish
        FROM heat_entries he
        JOIN heats h ON h.id = he.heat_id
        WHERE h.tournament_id = ? AND h.stage = 'staging' AND h.round = ? AND he.finish IS NOT NULL
        """,
        (tournament_id, round),
    ):
        finishes_by_racer.setdefault(finish_row["racer_id"], []).append(finish_row["finish"])
    max_place = max(
        (finish for finishes in finishes_by_racer.values() for finish in finishes if finish > 0),
        default=0,
    )

    placement_vector_by_id: dict[int, tuple[int, ...]] = {}
    for row in rows:
        racer_finishes = finishes_by_racer.get(row["id"], [])
        placement_counts = Counter(finish for finish in racer_finishes if finish > 0)
        dnf_count = sum(1 for finish in racer_finishes if finish == 0)
        # Negated so more of a good placement sorts first; DNF count is left
        # positive since fewer of those is what's better.
        placement_vector_by_id[row["id"]] = tuple(
            -placement_counts.get(place, 0) for place in range(1, max_place + 1)
        ) + (dnf_count,)

    if tie_override is None:
        tie_override = {
            row["racer_id"]: row["resolved_rank"]
            for row in connection.execute(
                "SELECT racer_id, resolved_rank FROM round_tiebreaks WHERE tournament_id = ? AND round = ?",
                (tournament_id, round),
            )
        }
    unresolved = len(rows) + 1  # sorts after every real manual rank

    ordered = sorted(
        rows,
        key=lambda row: (
            -row["total_points"],
            placement_vector_by_id[row["id"]],
            tie_override.get(row["id"], unresolved),
            row["sort_order"],
        ),
    )
    return [
        {
            "rank": rank,
            "id": row["id"],
            "name": row["name"],
            "color": row["color"],
            "totalPoints": row["total_points"],
            "wins": -placement_vector_by_id[row["id"]][0] if max_place else 0,
            "placementVector": placement_vector_by_id[row["id"]],
        }
        for rank, row in enumerate(ordered, start=1)
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


def is_staging_round_complete(connection: sqlite3.Connection, tournament_id: int, round: int) -> bool:
    """A staging round is complete once every heat scheduled for it has every
    entry scored -- a round with heats still pending isn't done racing, so
    its standings shouldn't be treated as final yet."""
    row = connection.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN he.finish IS NOT NULL THEN 1 ELSE 0 END) AS finished
        FROM heats h
        JOIN heat_entries he ON he.heat_id = h.id
        WHERE h.tournament_id = ? AND h.stage = 'staging' AND h.round = ?
        """,
        (tournament_id, round),
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


def is_heat_locked(connection: sqlite3.Connection, tournament_id: int, global_number: int) -> bool:
    """A heat is locked while any earlier heat in the tournament (by
    global_number, the canonical race order spanning every stage) still has
    unscored entries -- or while an earlier staging round has a promotion tie
    still waiting on the organizer to resolve it, since that round's results
    aren't really final yet either."""
    row = connection.execute(
        """
        SELECT 1
        FROM heats h
        WHERE h.tournament_id = ? AND h.global_number < ?
          AND EXISTS (SELECT 1 FROM heat_entries he WHERE he.heat_id = h.id AND he.finish IS NULL)
        LIMIT 1
        """,
        (tournament_id, global_number),
    ).fetchone()
    if row is not None:
        return True

    pending = pending_round_tiebreak(connection, tournament_id)
    if pending is None:
        return False
    last_heat_of_pending_round = connection.execute(
        "SELECT MAX(global_number) AS max_global FROM heats WHERE tournament_id = ? AND round = ?",
        (tournament_id, pending["round"]),
    ).fetchone()
    return global_number > last_heat_of_pending_round["max_global"]


def is_heat_edit_locked(connection: sqlite3.Connection, tournament_id: int, global_number: int) -> bool:
    """A heat's results are locked for editing once any later heat in the
    tournament (by global_number, spanning every stage) has been started."""
    row = connection.execute(
        """
        SELECT 1
        FROM heats h
        WHERE h.tournament_id = ? AND h.global_number > ?
          AND h.started_at IS NOT NULL
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


def _cascade_select(
    pool: Sequence[dict[str, Any]],
    seats: int,
    cascade: bool,
    capacity_fn: Callable[[int], int],
    running_counts: dict[int, int],
) -> list[dict[str, Any]]:
    """Fill up to `seats` slots from pool (already in rank order).

    With cascading, scan down the pool until each seat is filled or the pool
    runs out -- a candidate who's already at their capacity is skipped in
    favor of the next-best finisher. Without cascading, each seat has
    exactly one fixed candidate (pool[i]) and is forfeited outright if that
    candidate is at capacity; no other rank is tried for that seat.
    """
    selected: list[dict[str, Any]] = []
    candidates = pool if cascade else pool[:seats]
    for rank_row in candidates:
        if len(selected) >= seats:
            break
        racer_id = rank_row["id"]
        if running_counts.get(racer_id, 0) < capacity_fn(racer_id):
            selected.append(rank_row)
            running_counts[racer_id] = running_counts.get(racer_id, 0) + 1
    return selected


def championship_field(
    connection: sqlite3.Connection,
    tournament_id: int,
    live_round: int | None = None,
    tie_overrides: dict[int, dict[int, int]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Pure computation over every staging round's round_standings().

    live_round, when given, forces that one round's current (possibly partial)
    round_standings() to be counted alongside every other already-complete
    round, even though it isn't finished racing itself -- used to preview what
    a round in progress would award if it ended right now.

    tie_overrides, when given, maps a round to a racer_id -> rank override for
    round_standings() on that round only -- every other round still uses its own
    persisted resolution (or seed order) as usual. This is how
    pending_round_tiebreak() tests a hypothetical resolution for one round's
    tie without writing anything to the database.

    Tier priority is bye > preliminary > wildcard, resolved in three full
    passes over every round (ascending), each seeing the previous pass's
    completed results. Each tier has its own per-racer marble cap and its
    own cascade toggle:

    - Bye: final_racers_promoted_per_round seats per round (default 1),
      normally its top finishers by rank. If a candidate is already at
      max_final_bye_marbles_per_racer and cascading is on, that seat
      cascades down the round's standings to the first racer under cap;
      with cascading off the seat is simply forfeited -- and either way,
      the rank position it was reserved for is never handed to a lower
      tier.
    - Preliminary: preliminary_racers_promoted_per_round seats per round
      (default 1) from the next ranks down (every rank reserved for bye
      above is always excluded, whether or not that bye seat was actually
      won there, and whoever actually won it -- which cascading can push
      further down -- is excluded too), capped at
      max_prelim_promotion_marbles_per_racer -- except a bye-tier racer's
      preliminary capacity is instead max_prelim_marbles_for_racer_with_final_bye
      (0 by default, i.e. still excluded unless raised).
    - Wildcard: wildcard_racers_promoted_per_round seats per round
      (default 2) from the next ranks down (again excluding every rank
      reserved for bye, plus whoever actually won that same round's bye
      and preliminary seats), capped at max_wildcard_promotion_marbles_per_racer
      -- except a bye-tier racer's wildcard capacity is
      max_wildcard_marbles_for_racer_with_final_bye, and a preliminary-tier
      racer's is max_wildcard_marbles_for_racer_with_prelim_promotion (both
      0 by default).

    A racer can never hold two tiers from the same round -- the bonus caps
    above only let a racer who earned a bye/preliminary seat in one round
    pick up additional marbles in a lower tier from a *different* round, up
    to that bonus cap across the tournament as a whole.

    In the shared-pool model every racer competes in every round, so the
    same racer can qualify for the same tier in more than one round. Rather
    than keeping only their single best slot, each racer's qualifying
    occurrences (by round ascending) become separate marbles in that tier's
    heat, up to that racer's capacity for the tier.
    """
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    bye_cap = tournament["max_final_bye_marbles_per_racer"]
    cascade_bye = bool(tournament["allow_cascading_final_bye_selection"])
    bye_prelim_bonus = tournament["max_prelim_marbles_for_racer_with_final_bye"]
    bye_wildcard_bonus = tournament["max_wildcard_marbles_for_racer_with_final_bye"]
    prelim_cap = tournament["max_prelim_promotion_marbles_per_racer"]
    cascade_prelim = bool(tournament["allow_cascading_prelim_promotion_selection"])
    prelim_wildcard_bonus = tournament["max_wildcard_marbles_for_racer_with_prelim_promotion"]
    wildcard_cap = tournament["max_wildcard_promotion_marbles_per_racer"]
    cascade_wildcard = bool(tournament["allow_cascading_wildcard_promotion_selection"])
    final_count = tournament["final_racers_promoted_per_round"]
    preliminary_count = tournament["preliminary_racers_promoted_per_round"]
    wildcard_count = tournament["wildcard_racers_promoted_per_round"]

    rankings: dict[int, list[dict[str, Any]]] = {}
    for round in range(1, tournament["rounds"] + 1):
        # A round isn't done racing until every one of its heats is scored --
        # round_standings() for a round still in progress (or untouched, where
        # it falls back to sort_order) isn't a final result and shouldn't
        # claim bye/preliminary/wildcard seats yet, unless it's the round
        # explicitly being previewed via live_round.
        if round != live_round and not is_staging_round_complete(connection, tournament_id, round):
            continue
        rankings[round] = round_standings(
            connection, tournament_id, round, tie_override=(tie_overrides or {}).get(round)
        )

    byes: list[dict[str, Any]] = []
    bye_counts: dict[int, int] = {}
    round_reserved_bye_ranks: dict[int, set[int]] = {}
    round_bye_winners: dict[int, set[int]] = {}
    for round in sorted(rankings):
        round_reserved_bye_ranks[round] = {row["id"] for row in rankings[round][:final_count]}
        winners: set[int] = set()
        for rank_row in _cascade_select(
            rankings[round], final_count, cascade_bye, lambda _rid: bye_cap, bye_counts
        ):
            winners.add(rank_row["id"])
            byes.append({"racerId": rank_row["id"], "originRound": round})
        round_bye_winners[round] = winners
    bye_racer_ids = {racer_id for racer_id, count in bye_counts.items() if count > 0}

    # A racer already claiming a higher tier this round is never a candidate
    # for a lower tier from the same round -- the bonus capacity settings
    # (bye_prelim_bonus etc.) only govern how many extra marbles a racer may
    # pick up across *other* rounds, not a second seat in this one. Every
    # rank reserved for bye is excluded outright since it's never handed to
    # a lower tier even when that seat is forfeited (capped with no
    # cascade).
    preliminary_direct: list[dict[str, Any]] = []
    prelim_counts: dict[int, int] = {}
    round_prelim_winners: dict[int, set[int]] = {}
    prelim_capacity = lambda rid: bye_prelim_bonus if rid in bye_racer_ids else prelim_cap
    for round in sorted(rankings):
        exclude = round_reserved_bye_ranks[round] | round_bye_winners[round]
        pool = [row for row in rankings[round] if row["id"] not in exclude]
        winners: set[int] = set()
        for rank_row in _cascade_select(pool, preliminary_count, cascade_prelim, prelim_capacity, prelim_counts):
            winners.add(rank_row["id"])
            preliminary_direct.append(
                {"racerId": rank_row["id"], "originRound": round, "points": rank_row["totalPoints"]}
            )
        round_prelim_winners[round] = winners
    prelim_racer_ids = {racer_id for racer_id, count in prelim_counts.items() if count > 0}

    wildcard_pool: list[dict[str, Any]] = []
    wildcard_counts: dict[int, int] = {}

    def wildcard_capacity(rid: int) -> int:
        if rid in bye_racer_ids:
            return bye_wildcard_bonus
        if rid in prelim_racer_ids:
            return prelim_wildcard_bonus
        return wildcard_cap

    for round in sorted(rankings):
        exclude = round_reserved_bye_ranks[round] | round_bye_winners[round] | round_prelim_winners[round]
        pool = [row for row in rankings[round] if row["id"] not in exclude]
        for rank_row in _cascade_select(pool, wildcard_count, cascade_wildcard, wildcard_capacity, wildcard_counts):
            wildcard_pool.append(
                {"racerId": rank_row["id"], "originRound": round, "points": rank_row["totalPoints"]}
            )

    return {"byes": byes, "preliminaryDirect": preliminary_direct, "wildcardPool": wildcard_pool}


def _championship_tier_snapshot(
    connection: sqlite3.Connection,
    tournament_id: int,
    tie_overrides: dict[int, dict[int, int]] | None = None,
) -> frozenset[tuple[str, int, int]]:
    """(tier, originRound, racerId) triples for every seat currently
    awarded. Diffing two of these -- one computed normally, one with a
    round's tied cluster hypothetically reordered -- is how
    pending_round_tiebreak() tells whether that tie actually changes who
    gets promoted, without having to reason by hand about how far a same-round
    tie's ripple effects (via the cross-round bonus-capacity caps) might
    reach into other rounds."""
    field = championship_field(connection, tournament_id, tie_overrides=tie_overrides)
    return frozenset(
        (tier, item["originRound"], item["racerId"])
        for tier, items in (
            ("bye", field["byes"]),
            ("preliminary", field["preliminaryDirect"]),
            ("wildcard", field["wildcardPool"]),
        )
        for item in items
    )


def _is_tie_group_consequential(
    connection: sqlite3.Connection,
    tournament_id: int,
    round: int,
    racer_ids: list[int],
    existing_round_override: dict[int, int],
    base_snapshot: frozenset[tuple[str, int, int]],
    extra_overrides: dict[int, dict[int, int]] | None = None,
) -> bool:
    """Whether reversing this one tied cluster's order -- holding every
    other round, and every other already-resolved cluster on this same round,
    fixed -- changes any racer's bye/preliminary/wildcard tier. A single round
    can hold more than one independent tied cluster (e.g. a bye-vs-
    preliminary tie between the top two racers, and a separate, unrelated
    tie further down that both already land on the same tier regardless of
    order) -- each is judged on its own, since one being consequential
    doesn't make the other one too. `existing_round_override` must already
    exclude this cluster's own racers so their hypothetical reversal isn't
    itself overridden away.

    `extra_overrides`, when given, is merged in underneath this cluster's own
    hypothetical reversal -- used to ask "is this cluster consequential in a
    world where some *other* round's tie has already been resolved a certain
    way," which must match whatever world `base_snapshot` was itself computed
    against."""
    hypothetical_round_override = dict(existing_round_override)
    hypothetical_round_override.update({rid: i for i, rid in enumerate(reversed(racer_ids))})
    tie_overrides = dict(extra_overrides or {})
    tie_overrides[round] = hypothetical_round_override
    alt_snapshot = _championship_tier_snapshot(connection, tournament_id, tie_overrides=tie_overrides)
    return base_snapshot != alt_snapshot


def _newly_consequential_earlier_ties(
    connection: sqlite3.Connection,
    tournament_id: int,
    pending_round: int,
    pending_override: dict[int, int],
) -> list[dict[str, Any]]:
    """Already-raced rounds before `pending_round` that hold a tied cluster which
    is harmless right now (so was left alone, falling back to roster order)
    but would need an organizer's manual call of its own once
    `pending_override` is applied to `pending_round` -- because the cross-round
    bonus-capacity ripple from that change makes the earlier cluster's order
    actually decide a seat for the first time. Returns one entry per round with
    a newly-consequential cluster, listing just the racers in that cluster."""
    extra_overrides = {pending_round: pending_override}
    base_snapshot = _championship_tier_snapshot(connection, tournament_id, tie_overrides=extra_overrides)
    results: list[dict[str, Any]] = []
    for candidate_round in range(1, pending_round):
        ranking = round_standings(connection, tournament_id, candidate_round)
        groups: dict[tuple[int, tuple[int, ...]], list[dict[str, Any]]] = {}
        for row in ranking:
            groups.setdefault((row["totalPoints"], row["placementVector"]), []).append(row)
        candidate_groups = [group for group in groups.values() if len(group) > 1]
        if not candidate_groups:
            continue
        existing_override = {
            row["racer_id"]: row["resolved_rank"]
            for row in connection.execute(
                "SELECT racer_id, resolved_rank FROM round_tiebreaks WHERE tournament_id = ? AND round = ?",
                (tournament_id, candidate_round),
            )
        }
        resolved_racer_ids = set(existing_override)
        newly_tied_racers: list[dict[str, Any]] = []
        for group in candidate_groups:
            racer_ids = [row["id"] for row in group]
            if resolved_racer_ids.issuperset(racer_ids):
                continue
            if _is_tie_group_consequential(
                connection, tournament_id, candidate_round, racer_ids, existing_override, base_snapshot,
                extra_overrides=extra_overrides,
            ):
                newly_tied_racers.extend(
                    {"id": row["id"], "name": row["name"], "color": row["color"]} for row in group
                )
        if newly_tied_racers:
            results.append({"round": candidate_round, "racers": newly_tied_racers})
    return results


def tiebreak_earlier_round_impact(
    connection: sqlite3.Connection, tournament_id: int, round: int, order: list[int]
) -> dict[str, Any]:
    """Whether applying `order` (best to worst) to round's tie changes the
    bye/preliminary/wildcard result of any round before `round` -- possible
    because bye_racer_ids/prelim_racer_ids in championship_field() are whole-
    tournament sets resolved before each tier's pass over every round, so
    which racer wins *this* round's bye or preliminary seat can retroactively
    change the bonus-capacity lookup an *earlier* round's own promotion
    depended on.

    Returns:
    - affectedRounds: sorted earlier round numbers whose seat assignments change.
    - seatChanges: one entry per (round, racer) whose tier flips, with the
      racer's name/color and the tier it moves from/to (None meaning no
      seat at all), so the organizer sees exactly which position moves
      rather than just which round is touched.
    - newTiebreaks: earlier rounds that currently have a harmless (unresolved,
      never-prompted) tied cluster which this change would turn
      consequential -- a heads-up that confirming here will surface another
      tiebreak prompt for that round next.

    Everything is empty if resolving this tie only ever affects round itself
    or later.
    """
    tie_override = {racer_id: rank for rank, racer_id in enumerate(order)}
    base_snapshot = _championship_tier_snapshot(connection, tournament_id)
    proposed_snapshot = _championship_tier_snapshot(
        connection, tournament_id, tie_overrides={round: tie_override}
    )
    base_by_seat = {(origin_round, racer_id): tier for tier, origin_round, racer_id in base_snapshot}
    proposed_by_seat = {(origin_round, racer_id): tier for tier, origin_round, racer_id in proposed_snapshot}
    changed_seats = {
        seat
        for seat in set(base_by_seat) | set(proposed_by_seat)
        if seat[0] < round and base_by_seat.get(seat) != proposed_by_seat.get(seat)
    }
    if not changed_seats:
        return {"affectedRounds": [], "seatChanges": [], "newTiebreaks": []}

    racers = {
        row["id"]: row
        for row in connection.execute(
            "SELECT id, name, color FROM racers WHERE tournament_id = ?", (tournament_id,)
        )
    }
    seat_changes = sorted(
        (
            {
                "round": origin_round,
                "racerId": racer_id,
                "racerName": racers[racer_id]["name"],
                "racerColor": racers[racer_id]["color"],
                "fromTier": base_by_seat.get((origin_round, racer_id)),
                "toTier": proposed_by_seat.get((origin_round, racer_id)),
            }
            for origin_round, racer_id in changed_seats
        ),
        key=lambda change: (change["round"], change["racerName"]),
    )
    return {
        "affectedRounds": sorted({change["round"] for change in seat_changes}),
        "seatChanges": seat_changes,
        "newTiebreaks": _newly_consequential_earlier_ties(connection, tournament_id, round, tie_override),
    }


def pending_round_tiebreak(
    connection: sqlite3.Connection, tournament_id: int
) -> dict[str, Any] | None:
    """The earliest staging round, if any, with a genuine tie (equal points and
    an identical placement vector -- same count of 1st-place finishes, same
    count of 2nd-place finishes, and so on) that would actually change who
    gets a bye/preliminary/wildcard seat depending on how it's broken -- one
    that currently falls back to roster order via round_standings() rather
    than to something the organizer chose.

    Rounds race in strict order and later heats stay locked while this is
    pending (see is_heat_locked()), so there's only ever one round to worry
    about at a time: nothing beyond it has raced yet, so there's nothing for
    this round's resolution to interact with beyond itself.

    Returns None once nothing is pending -- including when every remaining
    tie is one that doesn't matter (e.g. two racers tied for a place with no
    seat on the line), which is left to resolve via seed order same as
    before, no prompt needed.
    """
    tournament = connection.execute(
        "SELECT rounds FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    for round in range(1, tournament["rounds"] + 1):
        if not is_staging_round_complete(connection, tournament_id, round):
            break

        ranking = round_standings(connection, tournament_id, round)
        groups: dict[tuple[int, tuple[int, ...]], list[dict[str, Any]]] = {}
        for row in ranking:
            groups.setdefault((row["totalPoints"], row["placementVector"]), []).append(row)
        candidate_groups = [group for group in groups.values() if len(group) > 1]
        if not candidate_groups:
            continue

        existing_round_override = {
            row["racer_id"]: row["resolved_rank"]
            for row in connection.execute(
                "SELECT racer_id, resolved_rank FROM round_tiebreaks WHERE tournament_id = ? AND round = ?",
                (tournament_id, round),
            )
        }
        resolved_racer_ids = set(existing_round_override)
        base_snapshot: frozenset[tuple[str, int, int]] | None = None
        for group in candidate_groups:
            racer_ids = [row["id"] for row in group]
            if resolved_racer_ids.issuperset(racer_ids):
                continue
            if base_snapshot is None:
                base_snapshot = _championship_tier_snapshot(connection, tournament_id)
            if not _is_tie_group_consequential(
                connection, tournament_id, round, racer_ids, existing_round_override, base_snapshot
            ):
                continue

            current_tier = {
                racer_id: tier
                for tier, origin_round, racer_id in base_snapshot
                if origin_round == round and racer_id in racer_ids
            }
            return {
                "round": round,
                "racers": [
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "color": row["color"],
                        "currentTier": current_tier.get(row["id"]),
                    }
                    for row in group
                ],
            }

    return None


def find_in_progress_staging_round(connection: sqlite3.Connection, tournament_id: int) -> int | None:
    """The one staging round, if any, that has some but not all of its heats
    scored. Heats are locked to run in strict global order, so at most one
    round can ever be in this state at a time."""
    tournament = connection.execute(
        "SELECT rounds FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    for round in range(1, tournament["rounds"] + 1):
        if is_staging_round_complete(connection, tournament_id, round):
            continue
        ranking = round_standings(connection, tournament_id, round)
        return round if any(row["totalPoints"] > 0 for row in ranking) else None
    return None


def live_round_preview(connection: sqlite3.Connection, tournament_id: int) -> dict[str, Any] | None:
    """What the currently in-progress staging round would award if it ended
    right now: who's provisionally leading it (drives a "wins" asterisk) and
    who'd provisionally land in each championship tier (bye/preliminary/
    wildcard), independent of whether the leader is capped out of an actual
    bye marble. None if no staging round is in progress.
    """
    live_round = find_in_progress_staging_round(connection, tournament_id)
    if live_round is None:
        return None
    ranking = round_standings(connection, tournament_id, live_round)
    field = championship_field(connection, tournament_id, live_round=live_round)
    round_tiers: dict[tuple[int, int], str] = {}
    for item in field["byes"]:
        round_tiers[(item["originRound"], item["racerId"])] = "bye"
    for item in field["preliminaryDirect"]:
        round_tiers.setdefault((item["originRound"], item["racerId"]), "preliminary")
    for item in field["wildcardPool"]:
        round_tiers.setdefault((item["originRound"], item["racerId"]), "wildcard")
    tiers = {
        racer_id: tier
        for (round, racer_id), tier in round_tiers.items()
        if round == live_round
    }
    return {
        "round": live_round,
        "leaderId": ranking[0]["id"],
        "tiers": tiers,
        "roundTiers": round_tiers,
    }


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
    randomized-restart repeated-pairing search balanced_round_schedule performs
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
        INSERT INTO heats (tournament_id, stage, round, heat_number, global_number)
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
    tournament = connection.execute(
        "SELECT wildcard_racers_promoted_per_heat FROM tournaments WHERE id = ?",
        (tournament_id,),
    ).fetchone()
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
            # heat_top_n ranks distinct racers by aggregate points, so
            # multiple marbles from one racer can strengthen that racer's
            # result but cannot consume multiple qualifying places.
            for racer_id in heat_top_n(
                connection, heat_row["id"], tournament["wildcard_racers_promoted_per_heat"]
            ):
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


def _final_candidate_pool(
    connection: sqlite3.Connection, tournament_id: int, tournament: sqlite3.Row
) -> tuple[list[dict[str, Any]], int]:
    """Bye winners plus preliminary-heat qualifiers (or a stage-skip fallback
    when there are no preliminary heats), deduped by racer -- the shared
    starting pool for whichever bracket stage (quarterfinal, semifinal, or a
    direct final) ends up racing it. Capped at 4x max_final_racers by
    standings rank as a last-resort safety net for fields too large to
    bracket down to size even via quarterfinals; returns how many were
    trimmed by that safety net (0 in the overwhelmingly common case).
    """
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
            for racer_id in heat_top_n(
                connection,
                heat_row["id"],
                tournament["preliminary_racers_promoted_per_heat"],
            ):
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

    safety_cap = tournament["max_final_racers"] * 4
    trimmed = 0
    if len(deduped) > safety_cap:
        overall = {row["id"]: row for row in standings(connection, tournament_id)}
        deduped.sort(
            key=lambda candidate: overall.get(candidate["racerId"], {}).get("rank", 10 ** 6)
        )
        trimmed = len(deduped) - safety_cap
        deduped = deduped[:safety_cap]
    return deduped, trimmed


def _seed_split_groups(
    connection: sqlite3.Connection,
    tournament_id: int,
    candidates: Sequence[dict[str, Any]],
    heat_count: int,
    seed: int,
) -> list[list[dict[str, Any]]]:
    """Deal candidates into heat_count bracket heats, balanced by overall
    standings rank (the same priority final_field used for trimming before
    this feature) rather than by origin -- each racer is its own bucket, so
    interleave_groups() degrades to round-robin dealing in rank order
    (rank 0,1,2,... -> heat 0,1,2,...,0,1,...), spreading the strongest
    racers evenly across heats instead of stacking them in one.
    """
    overall = {row["id"]: row for row in standings(connection, tournament_id)}
    ordered = sorted(
        candidates, key=lambda candidate: overall.get(candidate["racerId"], {}).get("rank", 10 ** 6)
    )
    return interleave_groups(ordered, heat_count, seed, lambda candidate: candidate["racerId"])


def quarterfinal_field(connection: sqlite3.Connection, tournament_id: int) -> list[dict[str, Any]]:
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    candidates, _trimmed = _final_candidate_pool(connection, tournament_id, tournament)
    if final_bracket_stage(len(candidates), tournament["max_final_racers"]) != "quarterfinal":
        return []
    return candidates


def build_quarterfinal_heats(connection: sqlite3.Connection, tournament_id: int) -> None:
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    entries = quarterfinal_field(connection, tournament_id)
    if not entries:
        return
    global_number = _next_global_number(connection, tournament_id)
    groups = [
        group
        for group in _seed_split_groups(connection, tournament_id, entries, 4, tournament["seed"] + 301)
        if group
    ]
    for heat_number, group in enumerate(groups, start=1):
        global_number += 1
        _insert_championship_heat(connection, tournament_id, "quarterfinal", heat_number, global_number, group)


def semifinal_field(connection: sqlite3.Connection, tournament_id: int) -> list[dict[str, Any]]:
    """Dual path, mirroring preliminary_field(): if quarterfinal heats exist,
    each pair of them (1&2, 3&4) feeds one semifinal heat via heat_top_n,
    splitting max_final_racers between the pair (remainder to the first
    heat) so each semifinal heat comes out no larger than max_final_racers.
    Each promoted candidate carries targetHeat (1 or 2) so
    build_semifinal_heats() can place it correctly without re-deriving the
    quarterfinal pairing. Otherwise, this is a direct bracket split straight
    off the bye/preliminary pool, same as quarterfinal_field's source.
    """
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    max_final = tournament["max_final_racers"]
    quarterfinal_heats = connection.execute(
        "SELECT id, heat_number FROM heats WHERE tournament_id = ? AND stage = 'quarterfinal' ORDER BY heat_number",
        (tournament_id,),
    ).fetchall()
    if quarterfinal_heats:
        candidates: list[dict[str, Any]] = []
        for pair_index in range(0, len(quarterfinal_heats), 2):
            pair = quarterfinal_heats[pair_index : pair_index + 2]
            target_heat = pair_index // 2 + 1
            base, extra = divmod(max_final, len(pair))
            for offset, heat_row in enumerate(pair):
                promote_n = base + (1 if offset < extra else 0)
                for racer_id in heat_top_n(connection, heat_row["id"], promote_n):
                    candidates.append(
                        {
                            "racerId": racer_id,
                            "originStage": "quarterfinal",
                            "originHeatId": heat_row["id"],
                            "targetHeat": target_heat,
                        }
                    )
        return candidates

    candidates, _trimmed = _final_candidate_pool(connection, tournament_id, tournament)
    if final_bracket_stage(len(candidates), max_final) != "semifinal":
        return []
    return candidates


def build_semifinal_heats(connection: sqlite3.Connection, tournament_id: int) -> None:
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    entries = semifinal_field(connection, tournament_id)
    if not entries:
        return
    global_number = _next_global_number(connection, tournament_id)
    if "targetHeat" in entries[0]:
        groups: list[list[dict[str, Any]]] = [[], []]
        for entry in entries:
            groups[entry["targetHeat"] - 1].append(entry)
    else:
        groups = _seed_split_groups(connection, tournament_id, entries, 2, tournament["seed"] + 302)
    for heat_number, group in enumerate((group for group in groups if group), start=1):
        global_number += 1
        _insert_championship_heat(connection, tournament_id, "semifinal", heat_number, global_number, group)


def final_field(
    connection: sqlite3.Connection, tournament_id: int
) -> tuple[list[dict[str, Any]], int]:
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    max_final = tournament["max_final_racers"]
    pool, trimmed = _final_candidate_pool(connection, tournament_id, tournament)

    semifinal_heats = connection.execute(
        "SELECT id, heat_number FROM heats WHERE tournament_id = ? AND stage = 'semifinal' ORDER BY heat_number",
        (tournament_id,),
    ).fetchall()
    if semifinal_heats:
        base, extra = divmod(max_final, len(semifinal_heats))
        candidates: list[dict[str, Any]] = []
        for offset, heat_row in enumerate(semifinal_heats):
            promote_n = base + (1 if offset < extra else 0)
            for racer_id in heat_top_n(connection, heat_row["id"], promote_n):
                candidates.append(
                    {"racerId": racer_id, "originStage": "semifinal", "originHeatId": heat_row["id"]}
                )
        return candidates, trimmed

    if final_bracket_stage(len(pool), max_final) != "final":
        return [], trimmed
    return pool, trimmed


def wildcard_groups_with_marble_splitting(
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
        total_marbles, tournament["wildcard_max_marbles_per_heat"], 1
    )
    if heat_count == 0:
        return
    global_number = _next_global_number(connection, tournament_id)
    groups = [
        group
        for group in wildcard_groups_with_marble_splitting(entries, heat_count, tournament["seed"])
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
        total_marbles, tournament["preliminary_max_marbles_per_heat"], 1
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
    max_marbles_column = "wildcard_max_marbles_per_heat" if stage == "wildcard" else "preliminary_max_marbles_per_heat"
    _marbles_per_heat, heat_count = calculate_championship_heat_size(
        field_size, tournament_row[max_marbles_column], 1
    )
    if heat_count == 0:
        return True
    return is_stage_complete(connection, tournament_id, stage)


def _bracket_stage_ready_to_advance(
    connection: sqlite3.Connection, tournament_id: int, stage: str, entries: Sequence[dict[str, Any]]
) -> bool:
    """Like _stage_ready_to_advance, but for quarterfinal/semifinal: those
    stages are sized by candidate_count // 4 or // 2 rather than a
    *_max_marbles_per_heat column, and final_bracket_stage() (via
    quarterfinal_field()/semifinal_field() returning an empty list when this
    round of the bracket isn't needed) already decided whether the stage
    exists at all -- so readiness is just "not needed" or "heats scored".
    """
    if not entries:
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


def _stage_heat_count(connection: sqlite3.Connection, tournament_id: int, stage: str) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM heats WHERE tournament_id = ? AND stage = ?", (tournament_id, stage)
    ).fetchone()[0]


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
    staging_total = tournament["rounds"] * tournament["heats_per_round"]
    if staging_total == 0 or completed_heat_count(connection, tournament_id, stage="staging") != staging_total:
        delete_championship_stages(connection, tournament_id, "wildcard")
        return

    wildcard_entries = wildcard_field(connection, tournament_id)
    wildcard_marbles = _field_marble_signature(wildcard_entries, "marbleSlots")
    current_wildcard_marbles = stage_racer_marbles(connection, tournament_id, "wildcard")
    total_wildcard_marbles = sum(entry["marbleSlots"] for entry in wildcard_entries)
    # A field-signature match alone isn't enough -- the same racers/marbles
    # can still be stale if wildcard_max_marbles_per_heat changed since the
    # heats were built, since that changes how many heats they should be
    # split across without touching who qualifies.
    _wildcard_heat_size, expected_wildcard_heat_count = calculate_championship_heat_size(
        total_wildcard_marbles, tournament["wildcard_max_marbles_per_heat"], 1
    )
    if (
        wildcard_marbles != (current_wildcard_marbles or [])
        or expected_wildcard_heat_count != _stage_heat_count(connection, tournament_id, "wildcard")
    ):
        delete_championship_stages(connection, tournament_id, "wildcard")
        if wildcard_marbles:
            build_wildcard_heats(connection, tournament_id)
    if not _stage_ready_to_advance(connection, tournament_id, "wildcard", total_wildcard_marbles, tournament):
        delete_championship_stages(connection, tournament_id, "preliminary")
        return

    preliminary_entries = preliminary_field(connection, tournament_id)
    preliminary_marbles = _field_marble_signature(preliminary_entries, "marbleSlots")
    current_preliminary_marbles = stage_racer_marbles(connection, tournament_id, "preliminary")
    total_preliminary_marbles = sum(entry["marbleSlots"] for entry in preliminary_entries)
    _preliminary_heat_size, expected_preliminary_heat_count = calculate_championship_heat_size(
        total_preliminary_marbles, tournament["preliminary_max_marbles_per_heat"], 1
    )
    if (
        preliminary_marbles != (current_preliminary_marbles or [])
        or expected_preliminary_heat_count != _stage_heat_count(connection, tournament_id, "preliminary")
    ):
        delete_championship_stages(connection, tournament_id, "preliminary")
        if preliminary_marbles:
            build_preliminary_heats(connection, tournament_id)
    if not _stage_ready_to_advance(connection, tournament_id, "preliminary", total_preliminary_marbles, tournament):
        delete_championship_stages(connection, tournament_id, "quarterfinal")
        return

    quarterfinal_entries = quarterfinal_field(connection, tournament_id)
    quarterfinal_ids = _field_marble_signature(quarterfinal_entries)
    current_quarterfinal_ids = stage_racer_marbles(connection, tournament_id, "quarterfinal")
    if quarterfinal_ids != (current_quarterfinal_ids or []):
        delete_championship_stages(connection, tournament_id, "quarterfinal")
        if quarterfinal_ids:
            build_quarterfinal_heats(connection, tournament_id)
    if not _bracket_stage_ready_to_advance(connection, tournament_id, "quarterfinal", quarterfinal_entries):
        delete_championship_stages(connection, tournament_id, "semifinal")
        return

    semifinal_entries = semifinal_field(connection, tournament_id)
    semifinal_ids = _field_marble_signature(semifinal_entries)
    current_semifinal_ids = stage_racer_marbles(connection, tournament_id, "semifinal")
    if semifinal_ids != (current_semifinal_ids or []):
        delete_championship_stages(connection, tournament_id, "semifinal")
        if semifinal_ids:
            build_semifinal_heats(connection, tournament_id)
    if not _bracket_stage_ready_to_advance(connection, tournament_id, "semifinal", semifinal_entries):
        delete_championship_stages(connection, tournament_id, "final")
        return

    final_candidates, _trimmed = final_field(connection, tournament_id)
    final_ids = _field_marble_signature(final_candidates)
    current_final_ids = stage_racer_marbles(connection, tournament_id, "final")
    if final_ids != (current_final_ids or []):
        delete_championship_stages(connection, tournament_id, "final")
        if final_ids:
            build_final_heat(connection, tournament_id)
