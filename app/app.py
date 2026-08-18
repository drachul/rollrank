from __future__ import annotations

import io
import re
from typing import Any

from flask import Flask, jsonify, request, send_file

from db import (
    completed_heat_count,
    connect,
    create_tournament,
    init_db,
    rebuild_schedule,
    standings,
    sync_final,
    transaction,
)
from report import build_report


app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["JSON_SORT_KEYS"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
init_db()

COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_RACERS_PER_HEAT = 24
MAX_MARBLES_PER_HEAT = 480


class ApiError(ValueError):
    def __init__(self, message: str, status: int = 400, **details: Any) -> None:
        super().__init__(message)
        self.status = status
        self.details = details


@app.errorhandler(ApiError)
def handle_api_error(error: ApiError):
    return jsonify({"error": str(error), **error.details}), error.status


@app.errorhandler(404)
def handle_not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "API endpoint not found."}), 404
    return app.send_static_file("index.html")


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/workspace")
def workspace():
    return app.send_static_file("index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


def resolve_tournament_id(connection, value: Any = None) -> int:
    candidate = value if value is not None else request.args.get("tournamentId")
    if candidate in (None, ""):
        row = connection.execute("SELECT id FROM tournaments ORDER BY id LIMIT 1").fetchone()
        if not row:
            raise ApiError("No tournaments are available.", status=404)
        return int(row["id"])
    try:
        tournament_id = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ApiError("Tournament id must be a whole number.") from exc
    if not connection.execute(
        "SELECT 1 FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone():
        raise ApiError("Tournament not found.", status=404)
    return tournament_id


def tournament_summaries(connection) -> list[dict[str, Any]]:
    result = []
    for row in connection.execute(
        """
        SELECT id, name, days, heats_per_day, updated_at
        FROM tournaments
        ORDER BY created_at, id
        """
    ):
        tournament_id = int(row["id"])
        total_heats = row["days"] * row["heats_per_day"]
        completed_heats = completed_heat_count(connection, tournament_id)
        final_counts = connection.execute(
            """
            SELECT COUNT(*) AS racers,
                   SUM(CASE WHEN finish IS NOT NULL THEN 1 ELSE 0 END) AS finished
            FROM final_entries
            WHERE tournament_id = ?
            """,
            (tournament_id,),
        ).fetchone()
        table = standings(connection, tournament_id)
        leader = table[0] if completed_heats and table else None
        result.append(
            {
                "id": tournament_id,
                "name": row["name"],
                "completedHeats": completed_heats,
                "totalHeats": total_heats,
                "finalComplete": bool(final_counts["racers"])
                and final_counts["racers"] == final_counts["finished"],
                "leader": (
                    {
                        "name": leader["name"],
                        "color": leader["color"],
                        "totalPoints": leader["totalPoints"],
                    }
                    if leader
                    else None
                ),
                "updatedAt": row["updated_at"],
            }
        )
    return result


def build_state(connection, tournament_id: int) -> dict[str, Any]:
    tournament_row = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    if not tournament_row:
        raise ApiError("Tournament not found.", status=404)
    tournament = dict(tournament_row)
    contestants = [
        dict(row)
        for row in connection.execute(
            """
            SELECT id, name, color, sort_order
            FROM racers
            WHERE tournament_id = ?
            ORDER BY sort_order
            """,
            (tournament_id,),
        )
    ]
    points = [
        row["points"]
        for row in connection.execute(
            """
            SELECT points FROM point_values
            WHERE tournament_id = ?
            ORDER BY place
            """,
            (tournament_id,),
        )
    ]
    heat_rows = connection.execute(
        """
        SELECT h.id, h.day, h.heat_number, h.global_number,
               he.lane, he.marble_number, he.finish, he.points,
               r.id AS racer_id, r.name, r.color
        FROM heats h
        JOIN heat_entries he ON he.heat_id = h.id
        JOIN racers r ON r.id = he.racer_id
        WHERE h.tournament_id = ?
        ORDER BY h.day, h.heat_number, he.lane, he.marble_number
        """,
        (tournament_id,),
    ).fetchall()
    heat_map: dict[int, dict[str, Any]] = {}
    entry_map: dict[tuple[int, int], dict[str, Any]] = {}
    for row in heat_rows:
        heat = heat_map.setdefault(
            row["id"],
            {
                "id": row["id"],
                "day": row["day"],
                "heatNumber": row["heat_number"],
                "globalNumber": row["global_number"],
                "entries": [],
            },
        )
        entry_key = (row["id"], row["lane"])
        entry = entry_map.get(entry_key)
        if entry is None:
            entry = {
                "lane": row["lane"],
                "contestantId": row["racer_id"],
                "name": row["name"],
                "color": row["color"],
                "marbles": [],
            }
            entry_map[entry_key] = entry
            heat["entries"].append(entry)
        entry["marbles"].append(
            {
                "number": row["marble_number"],
                "finish": row["finish"],
                "points": row["points"],
            }
        )
    days = []
    for day in range(1, tournament["days"] + 1):
        day_heats = [heat for heat in heat_map.values() if heat["day"] == day]
        for heat in day_heats:
            for entry in heat["entries"]:
                entry["complete"] = all(
                    marble["finish"] is not None for marble in entry["marbles"]
                )
                entry["finish"] = (
                    entry["marbles"][0]["finish"]
                    if len(entry["marbles"]) == 1
                    else None
                )
                entry["points"] = (
                    sum(marble["points"] or 0 for marble in entry["marbles"])
                    if any(marble["points"] is not None for marble in entry["marbles"])
                    else None
                )
            heat["complete"] = all(entry["complete"] for entry in heat["entries"])
        days.append({"day": day, "heats": day_heats})

    table = standings(connection, tournament_id)
    total_heats = tournament["days"] * tournament["heats_per_day"]
    completed_heats = completed_heat_count(connection, tournament_id)
    final_rows = connection.execute(
        """
        SELECT fe.seed, fe.finish, r.id AS racer_id, r.name, r.color
        FROM final_entries fe
        JOIN racers r ON r.id = fe.racer_id
        WHERE fe.tournament_id = ?
        ORDER BY fe.seed
        """,
        (tournament_id,),
    ).fetchall()
    final_racers = []
    standings_by_id = {row["id"]: row for row in table}
    for row in final_rows:
        standing = standings_by_id[row["racer_id"]]
        final_racers.append(
            {
                "seed": row["seed"],
                "contestantId": row["racer_id"],
                "name": row["name"],
                "color": row["color"],
                "totalPoints": standing["totalPoints"],
                "finish": row["finish"],
            }
        )
    champion = next((row for row in final_racers if row["finish"] == 1), None)
    return {
        "competition": {
            "id": tournament_id,
            "name": tournament["name"],
            "days": tournament["days"],
            "heatsPerDay": tournament["heats_per_day"],
            "heatsPerRacerPerDay": tournament["heats_per_racer_per_day"],
            "racersPerHeat": tournament["racers_per_heat"],
            "maxMarblesPerHeat": tournament["max_marbles_per_heat"],
            "marblesPerHeat": tournament["racers_per_heat"]
            * tournament["marbles_per_racer"],
            "marblesPerRacer": tournament["marbles_per_racer"],
            "championshipRacers": tournament["final_racers"],
            "totalHeats": total_heats,
            "completedHeats": completed_heats,
        },
        "tournaments": tournament_summaries(connection),
        "contestants": contestants,
        "points": points,
        "days": days,
        "standings": table,
        "championship": {
            "ready": completed_heats == total_heats,
            "complete": bool(final_racers)
            and all(row["finish"] is not None for row in final_racers),
            "racers": final_racers,
            "champion": champion,
        },
    }


@app.get("/api/state")
def get_state():
    connection = connect()
    try:
        tournament_id = resolve_tournament_id(connection)
        return jsonify(build_state(connection, tournament_id))
    finally:
        connection.close()


def integer_field(
    data: dict[str, Any], name: str, minimum: int, maximum: int, label: str | None = None
) -> int:
    display_name = label or name
    try:
        value = int(data.get(name))
    except (TypeError, ValueError) as exc:
        raise ApiError(f"{display_name} must be a whole number.") from exc
    if not minimum <= value <= maximum:
        raise ApiError(f"{display_name} must be between {minimum} and {maximum}.")
    return value


def validated_tournament_name(value: Any) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 80:
        raise ApiError("Tournament name must be between 1 and 80 characters.")
    return name


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
    raise ApiError(
        "No full heat schedule fits this maximum. Increase max marbles per heat "
        "or adjust heats per racer per round."
    )


def validate_configuration(data: dict[str, Any]) -> dict[str, Any]:
    name = validated_tournament_name(data.get("name"))
    contestants_input = data.get("contestants")
    if not isinstance(contestants_input, list) or not 2 <= len(contestants_input) <= 32:
        raise ApiError("Supply between 2 and 32 racers.")
    contestants = []
    seen_names: set[str] = set()
    for index, item in enumerate(contestants_input):
        if not isinstance(item, dict):
            raise ApiError("Each racer must include a name and color.")
        contestant_name = str(item.get("name", "")).strip()
        color = str(item.get("color", "")).strip().upper()
        if not 1 <= len(contestant_name) <= 50:
            raise ApiError(f"Racer {index + 1} needs a name of 1 to 50 characters.")
        if contestant_name.casefold() in seen_names:
            raise ApiError(f"Racer names must be unique: {contestant_name}.")
        if not COLOR_PATTERN.match(color):
            raise ApiError(f"Invalid color for {contestant_name}.")
        seen_names.add(contestant_name.casefold())
        contestants.append({"name": contestant_name, "color": color})

    days = integer_field(data, "days", 1, 30, "Race rounds")
    normalized_data = {
        **data,
        "marblesPerRacer": data.get("marblesPerRacer", 1),
    }
    marbles_per_racer = integer_field(
        normalized_data, "marblesPerRacer", 1, 20, "Marbles per racer per heat"
    )
    heats_per_racer_per_day = integer_field(
        data, "heatsPerRacerPerDay", 1, 20, "Heats per racer per round"
    )
    max_marbles_per_heat = integer_field(
        data, "maxMarblesPerHeat", 2, MAX_MARBLES_PER_HEAT, "Max marbles per heat"
    )
    racers_per_heat = calculate_racers_per_heat(
        len(contestants),
        heats_per_racer_per_day,
        max_marbles_per_heat,
        marbles_per_racer,
    )
    final_racers = integer_field(
        data, "championshipRacers", 2, min(24, len(contestants))
    )
    slots_per_day = len(contestants) * heats_per_racer_per_day
    heats_per_day = slots_per_day // racers_per_heat
    if days * heats_per_day > 600:
        raise ApiError(
            "This tournament creates more than 600 heats. Reduce the rounds or heats per racer."
        )
    points_input = data.get("points")
    if not isinstance(points_input, list):
        raise ApiError("Points must be supplied as a list.")
    points = []
    for index in range(racers_per_heat * marbles_per_racer):
        try:
            value = int(points_input[index]) if index < len(points_input) else 0
        except (TypeError, ValueError) as exc:
            raise ApiError(f"Points for place {index + 1} must be a whole number.") from exc
        if not 0 <= value <= 10000:
            raise ApiError("Points must be between 0 and 10,000.")
        points.append(value)
    return {
        "name": name,
        "days": days,
        "heatsPerDay": heats_per_day,
        "heatsPerRacerPerDay": heats_per_racer_per_day,
        "racersPerHeat": racers_per_heat,
        "maxMarblesPerHeat": max_marbles_per_heat,
        "marblesPerRacer": marbles_per_racer,
        "championshipRacers": final_racers,
        "contestants": contestants,
        "points": points,
        "confirmReset": data.get("confirmReset") is True,
    }


@app.get("/api/tournaments")
def list_tournaments():
    connection = connect()
    try:
        return jsonify({"tournaments": tournament_summaries(connection)})
    finally:
        connection.close()


@app.post("/api/tournaments")
def add_tournament():
    payload = request.get_json(silent=True) or {}
    name = validated_tournament_name(payload.get("name"))
    with transaction() as connection:
        if connection.execute(
            "SELECT 1 FROM tournaments WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone():
            raise ApiError("A tournament with that name already exists.", status=409)
        tournament_id = create_tournament(connection, name)
        return jsonify(build_state(connection, tournament_id)), 201


def update_tournament(tournament_id: int):
    data = validate_configuration(request.get_json(silent=True) or {})
    with transaction() as connection:
        tournament_id = resolve_tournament_id(connection, tournament_id)
        duplicate_name = connection.execute(
            """
            SELECT 1 FROM tournaments
            WHERE name = ? COLLATE NOCASE AND id != ?
            """,
            (data["name"], tournament_id),
        ).fetchone()
        if duplicate_name:
            raise ApiError("A tournament with that name already exists.", status=409)
        current = connection.execute(
            "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
        ).fetchone()
        current_racers = connection.execute(
            """
            SELECT * FROM racers
            WHERE tournament_id = ?
            ORDER BY sort_order
            """,
            (tournament_id,),
        ).fetchall()
        current_points = [
            row["points"]
            for row in connection.execute(
                """
                SELECT points FROM point_values
                WHERE tournament_id = ?
                ORDER BY place
                """,
                (tournament_id,),
            )
        ]
        structure_changed = (
            current["days"] != data["days"]
            or current["heats_per_racer_per_day"] != data["heatsPerRacerPerDay"]
            or current["racers_per_heat"] != data["racersPerHeat"]
            or current["marbles_per_racer"] != data["marblesPerRacer"]
            or current["final_racers"] != data["championshipRacers"]
            or len(current_racers) != len(data["contestants"])
            or current_points != data["points"]
        )
        has_results = connection.execute(
            """
            SELECT 1
            FROM heat_entries he
            JOIN heats h ON h.id = he.heat_id
            WHERE h.tournament_id = ? AND he.finish IS NOT NULL
            LIMIT 1
            """,
            (tournament_id,),
        ).fetchone() is not None
        if structure_changed and has_results and not data["confirmReset"]:
            raise ApiError(
                "This change rebuilds this tournament's schedule and clears its heat results.",
                status=409,
                requiresReset=True,
            )

        connection.execute(
            """
            UPDATE tournaments
            SET name = ?, days = ?, heats_per_day = ?, heats_per_racer_per_day = ?,
                racers_per_heat = ?, max_marbles_per_heat = ?, marbles_per_racer = ?,
                final_racers = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                data["name"],
                data["days"],
                data["heatsPerDay"],
                data["heatsPerRacerPerDay"],
                data["racersPerHeat"],
                data["maxMarblesPerHeat"],
                data["marblesPerRacer"],
                data["championshipRacers"],
                tournament_id,
            ),
        )

        if len(current_racers) == len(data["contestants"]):
            for row in current_racers:
                connection.execute(
                    "UPDATE racers SET name = ? WHERE id = ?",
                    (f"__temporary_racer_{row['id']}__", row["id"]),
                )
            for row, racer in zip(current_racers, data["contestants"]):
                connection.execute(
                    "UPDATE racers SET name = ?, color = ? WHERE id = ?",
                    (racer["name"], racer["color"], row["id"]),
                )
        else:
            connection.execute(
                "DELETE FROM final_entries WHERE tournament_id = ?",
                (tournament_id,),
            )
            connection.execute("DELETE FROM heats WHERE tournament_id = ?", (tournament_id,))
            connection.execute(
                "DELETE FROM racers WHERE tournament_id = ?", (tournament_id,)
            )
            connection.executemany(
                """
                INSERT INTO racers (tournament_id, name, color, sort_order)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (tournament_id, racer["name"], racer["color"], index)
                    for index, racer in enumerate(data["contestants"])
                ],
            )

        connection.execute(
            "DELETE FROM point_values WHERE tournament_id = ?", (tournament_id,)
        )
        connection.executemany(
            "INSERT INTO point_values (tournament_id, place, points) VALUES (?, ?, ?)",
            [
                (tournament_id, place, value)
                for place, value in enumerate(data["points"], start=1)
            ],
        )
        if structure_changed:
            rebuild_schedule(connection, tournament_id)
        return jsonify(build_state(connection, tournament_id))


@app.put("/api/tournaments/<int:tournament_id>")
def update_tournament_route(tournament_id: int):
    return update_tournament(tournament_id)


@app.put("/api/competition")
def update_competition_compatibility():
    connection = connect()
    try:
        tournament_id = resolve_tournament_id(connection)
    finally:
        connection.close()
    return update_tournament(tournament_id)


@app.delete("/api/tournaments/<int:tournament_id>")
def delete_tournament(tournament_id: int):
    with transaction() as connection:
        tournament_id = resolve_tournament_id(connection, tournament_id)
        connection.execute("DELETE FROM tournaments WHERE id = ?", (tournament_id,))
        next_row = connection.execute(
            "SELECT id FROM tournaments ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        next_id = next_row["id"] if next_row else None
        return jsonify({"deletedId": tournament_id, "nextTournamentId": next_id})


@app.put("/api/heats/<int:heat_id>/results")
def save_heat_results(heat_id: int):
    payload = request.get_json(silent=True) or {}
    results = payload.get("results")
    if not isinstance(results, list):
        raise ApiError("Results must be supplied as a list.")
    with transaction() as connection:
        entries = connection.execute(
            """
            SELECT h.tournament_id, he.racer_id, he.marble_number
            FROM heat_entries he
            JOIN heats h ON h.id = he.heat_id
            WHERE he.heat_id = ?
            ORDER BY he.lane, he.marble_number
            """,
            (heat_id,),
        ).fetchall()
        if not entries:
            raise ApiError("Heat not found.", status=404)
        tournament_id = int(entries[0]["tournament_id"])
        expected_keys = {
            (row["racer_id"], row["marble_number"]) for row in entries
        }
        if len(results) != len(entries):
            raise ApiError("Enter a finishing position for every marble in the heat.")
        parsed: dict[tuple[int, int], int] = {}
        for result in results:
            try:
                racer_id = int(result.get("contestantId"))
                marble_number = int(result.get("marbleNumber", 1))
                finish = int(result.get("finish"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ApiError(
                    "Every result needs a racer, marble number, and finishing position."
                ) from exc
            parsed[(racer_id, marble_number)] = finish
        if set(parsed) != expected_keys:
            raise ApiError("The submitted racer marbles do not match this heat.")
        finishes = list(parsed.values())
        if any(finish < 0 or finish > len(entries) for finish in finishes):
            raise ApiError("Finishing positions must be a place in this heat or DNF.")
        placed_finishes = [finish for finish in finishes if finish > 0]
        expected_finishes = set(range(1, len(placed_finishes) + 1))
        if len(set(placed_finishes)) != len(placed_finishes) or set(placed_finishes) != expected_finishes:
            raise ApiError("Finishing positions must be unique and consecutive; DNF may be used more than once.")
        point_values = {
            row["place"]: row["points"]
            for row in connection.execute(
                """
                SELECT place, points FROM point_values
                WHERE tournament_id = ?
                """,
                (tournament_id,),
            )
        }
        for (racer_id, marble_number), finish in parsed.items():
            connection.execute(
                """
                UPDATE heat_entries SET finish = ?, points = ?
                WHERE heat_id = ? AND racer_id = ? AND marble_number = ?
                """,
                (
                    finish,
                    0 if finish == 0 else point_values.get(finish, 0),
                    heat_id,
                    racer_id,
                    marble_number,
                ),
            )
        sync_final(connection, tournament_id)
        return jsonify(build_state(connection, tournament_id))


def save_final_results(tournament_id: int):
    payload = request.get_json(silent=True) or {}
    results = payload.get("results")
    if not isinstance(results, list):
        raise ApiError("Final results must be supplied as a list.")
    with transaction() as connection:
        tournament_id = resolve_tournament_id(connection, tournament_id)
        tournament = connection.execute(
            "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
        ).fetchone()
        if completed_heat_count(connection, tournament_id) != (
            tournament["days"] * tournament["heats_per_day"]
        ):
            raise ApiError("Complete every round heat before scoring the final.")
        qualifiers = connection.execute(
            """
            SELECT racer_id FROM final_entries
            WHERE tournament_id = ?
            ORDER BY seed
            """,
            (tournament_id,),
        ).fetchall()
        expected_ids = {row["racer_id"] for row in qualifiers}
        if len(results) != len(qualifiers):
            raise ApiError("Enter a finishing position for every finalist.")
        parsed: dict[int, int] = {}
        for result in results:
            try:
                racer_id = int(result.get("contestantId"))
                finish = int(result.get("finish"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ApiError("Every final result needs a racer and position.") from exc
            parsed[racer_id] = finish
        if set(parsed) != expected_ids:
            raise ApiError("The submitted racers do not match the final field.")
        finishes = list(parsed.values())
        if any(finish < 0 or finish > len(qualifiers) for finish in finishes):
            raise ApiError("Final positions must be a place in this final or DNF.")
        placed_finishes = [finish for finish in finishes if finish > 0]
        if 1 not in placed_finishes:
            raise ApiError("The final must have exactly one first-place finisher.")
        expected_finishes = set(range(1, len(placed_finishes) + 1))
        if len(set(placed_finishes)) != len(placed_finishes) or set(placed_finishes) != expected_finishes:
            raise ApiError("Final positions must be unique and consecutive; DNF may be used more than once.")
        for racer_id, finish in parsed.items():
            connection.execute(
                """
                UPDATE final_entries SET finish = ?
                WHERE tournament_id = ? AND racer_id = ?
                """,
                (finish, tournament_id, racer_id),
            )
        return jsonify(build_state(connection, tournament_id))


@app.put("/api/tournaments/<int:tournament_id>/final/results")
def save_tournament_final_results(tournament_id: int):
    return save_final_results(tournament_id)


@app.put("/api/championship/results")
def save_final_results_compatibility():
    connection = connect()
    try:
        tournament_id = resolve_tournament_id(connection)
    finally:
        connection.close()
    return save_final_results(tournament_id)


def tournament_report(tournament_id: int):
    connection = connect()
    try:
        tournament_id = resolve_tournament_id(connection, tournament_id)
        state = build_state(connection, tournament_id)
    finally:
        connection.close()
    report_bytes = build_report(state)
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", state["competition"]["name"]).strip("_")
    return send_file(
        io.BytesIO(report_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"{safe_name or 'marble_tournament'}_report.pdf",
    )


@app.get("/api/tournaments/<int:tournament_id>/report.pdf")
def tournament_report_pdf(tournament_id: int):
    return tournament_report(tournament_id)


@app.get("/api/report.pdf")
def report_pdf_compatibility():
    connection = connect()
    try:
        tournament_id = resolve_tournament_id(connection)
    finally:
        connection.close()
    return tournament_report(tournament_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
