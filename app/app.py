from __future__ import annotations

import io
import json
import re
import time
from typing import Any

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

from db import (
    calculate_racers_per_heat,
    championship_field,
    completed_heat_count,
    connect,
    consolidate_by_racer,
    create_tournament,
    final_field,
    find_in_progress_staging_day,
    heat_top_n,
    init_db,
    is_heat_edit_locked,
    is_heat_locked,
    is_stage_complete,
    pending_round_tiebreak,
    preliminary_field,
    rebuild_schedule,
    stage_has_results,
    standings,
    sync_championship,
    transaction,
    wildcard_field,
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
        completed_heats = completed_heat_count(connection, tournament_id, stage="staging")
        table = standings(connection, tournament_id)
        leader = table[0] if completed_heats and table else None
        result.append(
            {
                "id": tournament_id,
                "name": row["name"],
                "completedHeats": completed_heats,
                "totalHeats": total_heats,
                "finalComplete": is_stage_complete(connection, tournament_id, "final"),
                "leader": (
                    {
                        "name": leader["name"],
                        "color": leader["color"],
                        "wins": leader["wins"],
                    }
                    if leader
                    else None
                ),
                "updatedAt": row["updated_at"],
            }
        )
    return result


def fetch_heat_rows(connection, tournament_id: int, stage: str):
    return connection.execute(
        """
        SELECT h.id, h.day, h.heat_number, h.global_number, h.stage, h.started_at,
               he.lane, he.marble_number, he.finish, he.points,
               he.origin_stage, he.origin_round, he.origin_heat_id,
               r.id AS racer_id, r.name, r.color
        FROM heats h
        JOIN heat_entries he ON he.heat_id = h.id
        JOIN racers r ON r.id = he.racer_id
        WHERE h.tournament_id = ? AND h.stage = ?
        ORDER BY h.day, h.heat_number, he.lane, he.marble_number
        """,
        (tournament_id, stage),
    ).fetchall()


def shape_heats(heat_rows) -> list[dict[str, Any]]:
    heat_map: dict[int, dict[str, Any]] = {}
    entry_map: dict[tuple[int, int], dict[str, Any]] = {}
    for row in heat_rows:
        heat = heat_map.get(row["id"])
        if heat is None:
            heat = {
                "id": row["id"],
                "day": row["day"],
                "heatNumber": row["heat_number"],
                "globalNumber": row["global_number"],
                "stage": row["stage"],
                "started": row["started_at"] is not None,
                "entries": [],
            }
            heat_map[row["id"]] = heat
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
            if row["origin_stage"] is not None:
                entry["originStage"] = row["origin_stage"]
                entry["originRound"] = row["origin_round"]
                entry["originHeatId"] = row["origin_heat_id"]
            entry_map[entry_key] = entry
            heat["entries"].append(entry)
        entry["marbles"].append(
            {
                "number": row["marble_number"],
                "finish": row["finish"],
                "points": row["points"],
                "originStage": row["origin_stage"],
                "originRound": row["origin_round"],
                "originHeatId": row["origin_heat_id"],
            }
        )
    heats = list(heat_map.values())
    for heat in heats:
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
    return heats


def apply_heat_locks(
    heats: list[dict[str, Any]], tie_locked_after_global: int | None = None
) -> None:
    """Sets `locked` (blocked from starting until earlier heats are scored)
    and `editLocked` (blocked from re-scoring once a later heat has started)
    on every heat, using the tournament-wide global_number order that spans
    staging and every championship stage.

    tie_locked_after_global, when given, is the highest global_number
    belonging to a staging day with an unresolved promotion tie -- every
    heat after it is locked too, mirroring is_heat_locked() in db.py so the
    UI doesn't show a heat as startable that the server would reject.
    """
    ordered = sorted(heats, key=lambda heat: heat["globalNumber"])
    started_globals = [heat["globalNumber"] for heat in ordered if heat["started"]]
    latest_started_global = max(started_globals) if started_globals else None
    blocked = False
    for heat in ordered:
        tie_blocked = (
            tie_locked_after_global is not None and heat["globalNumber"] > tie_locked_after_global
        )
        if heat["complete"]:
            heat["locked"] = False
        else:
            heat["locked"] = blocked or tie_blocked
            blocked = True
        heat["editLocked"] = (
            latest_started_global is not None and heat["globalNumber"] < latest_started_global
        )


def _resolve_seed_rounds(
    connection, origin_stage: str | None, origin_round: int | None, origin_heat_id: int | None, racer_id: int, depth: int = 0
) -> set[int]:
    """The staging round(s) that ultimately produced a marble. A marble
    seeded directly from a staging round (wildcard entries, and direct
    preliminary/bye qualifiers) already carries that round. A marble seeded
    by winning a prior championship heat (a preliminary entry advancing from
    wildcard, or a final entry advancing from preliminary) only carries a
    reference to that heat, so we look up the same racer's marbles there and
    resolve recursively -- at most two hops (wildcard -> preliminary -> final).
    """
    if origin_stage in ("staging-round", "bye"):
        return {origin_round} if origin_round is not None else set()
    if origin_heat_id is None or depth > 3:
        return set()
    rows = connection.execute(
        "SELECT DISTINCT origin_stage, origin_round, origin_heat_id FROM heat_entries WHERE heat_id = ? AND racer_id = ?",
        (origin_heat_id, racer_id),
    ).fetchall()
    rounds: set[int] = set()
    for row in rows:
        rounds |= _resolve_seed_rounds(
            connection, row["origin_stage"], row["origin_round"], row["origin_heat_id"], racer_id, depth + 1
        )
    return rounds


def attach_seed_rounds(connection, heats: list[dict[str, Any]]) -> None:
    """Annotate each championship-heat entry with seedRounds -- the sorted
    staging round(s) behind every marble it's racing, so the ladder can show
    where a multi-marble racer's marbles actually came from.
    """
    for heat in heats:
        for entry in heat["entries"]:
            if "originStage" not in entry:
                continue
            rounds: set[int] = set()
            for marble in entry["marbles"]:
                rounds |= _resolve_seed_rounds(
                    connection,
                    marble.get("originStage"),
                    marble.get("originRound"),
                    marble.get("originHeatId"),
                    entry["contestantId"],
                )
            entry["seedRounds"] = sorted(rounds)


def _projected_roster(
    connection,
    known_entries: list[dict[str, Any]],
    source_heats: list[dict[str, Any]],
    origin_stage: str,
    qualifiers_per_heat: int = 1,
) -> list[dict[str, Any]]:
    """The racers who will fill a locked stage, as far as that's knowable
    right now: racers who already directly qualified (known_entries), plus
    the configured number of racer slots per heat feeding into this stage --
    the actual qualifiers if that heat is scored, otherwise TBD placeholders.
    """
    roster = [{**entry, "decided": True} for entry in known_entries]
    for heat in source_heats:
        qualifier_entries = []
        if heat["complete"]:
            qualifier_ids = heat_top_n(connection, heat["id"], qualifiers_per_heat)
            qualifier_entries = [
                next(
                    (entry for entry in heat["entries"] if entry["contestantId"] == racer_id), None
                )
                for racer_id in qualifier_ids
            ]
        slot_count = min(qualifiers_per_heat, len(heat["entries"]))
        for index in range(slot_count):
            qualifier_entry = qualifier_entries[index] if index < len(qualifier_entries) else None
            if qualifier_entry:
                roster.append(
                    {
                        "contestantId": qualifier_entry["contestantId"],
                        "name": qualifier_entry["name"],
                        "color": qualifier_entry["color"],
                        "originStage": origin_stage,
                        "originHeatId": heat["id"],
                        "qualifyingPlace": index + 1,
                        "seedRounds": qualifier_entry.get("seedRounds", []),
                        "decided": True,
                    }
                )
            else:
                roster.append(
                    {
                        "originStage": origin_stage,
                        "originHeatId": heat["id"],
                        "heatNumber": heat["heatNumber"],
                        "qualifyingPlace": index + 1,
                        "decided": False,
                    }
                )
    return roster


def build_championship_state(connection, tournament_id: int, staging_ready: bool) -> dict[str, Any]:
    tournament = connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    wildcard_heats = shape_heats(fetch_heat_rows(connection, tournament_id, "wildcard")) if staging_ready else []
    attach_seed_rounds(connection, wildcard_heats)
    wildcard_complete = staging_ready and all(heat["complete"] for heat in wildcard_heats)
    wildcard_field_size = (
        sum(len(heat["entries"]) for heat in wildcard_heats)
        if wildcard_heats
        else (len(wildcard_field(connection, tournament_id)) if staging_ready else 0)
    )

    preliminary_ready = staging_ready and wildcard_complete
    preliminary_heats = (
        shape_heats(fetch_heat_rows(connection, tournament_id, "preliminary")) if preliminary_ready else []
    )
    attach_seed_rounds(connection, preliminary_heats)
    preliminary_complete = preliminary_ready and all(heat["complete"] for heat in preliminary_heats)
    preliminary_field_size = (
        sum(len(heat["entries"]) for heat in preliminary_heats)
        if preliminary_heats
        else (len(preliminary_field(connection, tournament_id)) if preliminary_ready else 0)
    )

    racer_lookup: dict[int, dict[str, Any]] = {}
    field = None
    preliminary_projected: list[dict[str, Any]] = []
    final_projected: list[dict[str, Any]] = []
    if staging_ready:
        racer_lookup = {
            row["id"]: {"name": row["name"], "color": row["color"]}
            for row in connection.execute("SELECT id, name, color FROM racers WHERE tournament_id = ?", (tournament_id,))
        }
        field = championship_field(connection, tournament_id)
        if not preliminary_ready:
            known_preliminary = [
                {
                    "contestantId": item["racerId"],
                    "name": racer_lookup[item["racerId"]]["name"],
                    "color": racer_lookup[item["racerId"]]["color"],
                    "originStage": "staging-round",
                    "originRound": item["originRound"],
                    "marbleSlots": item["marbleSlots"],
                    "seedRounds": sorted(
                        {origin["originRound"] for origin in item["marbleOrigins"] if origin["originRound"] is not None}
                    ),
                }
                for item in consolidate_by_racer(field["preliminaryDirect"])
            ]
            preliminary_projected = _projected_roster(
                connection,
                known_preliminary,
                wildcard_heats,
                "wildcard",
                qualifiers_per_heat=tournament["wildcard_racers_promoted_per_heat"],
            )

    final_ready = preliminary_ready and preliminary_complete
    if staging_ready and not final_ready:
        # The final always races one marble per racer even if a racer
        # banked multiple bye rounds, so the preview shows them once.
        known_final = [
            {
                "contestantId": item["racerId"],
                "name": racer_lookup[item["racerId"]]["name"],
                "color": racer_lookup[item["racerId"]]["color"],
                "originStage": "bye",
                "originRound": item["originRound"],
                "seedRounds": sorted(
                    {origin["originRound"] for origin in item["marbleOrigins"] if origin["originRound"] is not None}
                ),
            }
            for item in consolidate_by_racer(field["byes"])
        ]
        final_projected = _projected_roster(
            connection,
            known_final,
            preliminary_heats,
            "preliminary",
            qualifiers_per_heat=tournament["preliminary_racers_promoted_per_heat"],
        )

    final_heats = shape_heats(fetch_heat_rows(connection, tournament_id, "final")) if final_ready else []
    attach_seed_rounds(connection, final_heats)
    final_heat = final_heats[0] if final_heats else None
    final_complete = final_ready and final_heat is not None and final_heat["complete"]
    trimmed_count = 0
    if final_ready:
        _candidates, trimmed_count = final_field(connection, tournament_id)
    champion = None
    bye_count = 0
    if final_heat is not None:
        bye_count = sum(1 for entry in final_heat["entries"] if entry.get("originStage") == "bye")
        if final_complete:
            champion_entry = next(
                (entry for entry in final_heat["entries"] if entry.get("finish") == 1), None
            )
            if champion_entry:
                champion = {
                    "contestantId": champion_entry["contestantId"],
                    "name": champion_entry["name"],
                    "color": champion_entry["color"],
                    "finish": 1,
                }

    return {
        "wildcard": {
            "ready": staging_ready,
            "complete": wildcard_complete,
            "heats": wildcard_heats,
            "fieldSize": wildcard_field_size,
            "skipped": staging_ready and not wildcard_heats,
        },
        "preliminary": {
            "ready": preliminary_ready,
            "complete": preliminary_complete,
            "heats": preliminary_heats,
            "fieldSize": preliminary_field_size,
            "skipped": preliminary_ready and not preliminary_heats,
            "projectedEntries": preliminary_projected,
        },
        "final": {
            "ready": final_ready,
            "complete": final_complete,
            "heat": final_heat,
            "champion": champion,
            "byeCount": bye_count,
            "trimmedCount": trimmed_count,
            "projectedEntries": final_projected,
        },
    }


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
    staging_heats = shape_heats(fetch_heat_rows(connection, tournament_id, "staging"))
    table = standings(connection, tournament_id)
    total_heats = tournament["days"] * tournament["heats_per_day"]
    completed_heats = completed_heat_count(connection, tournament_id, stage="staging")
    staging_ready = total_heats > 0 and completed_heats == total_heats
    championship = build_championship_state(connection, tournament_id, staging_ready)

    pending_tie_break = pending_round_tiebreak(connection, tournament_id)
    tie_lock_boundary = None
    if pending_tie_break is not None:
        tie_lock_boundary = connection.execute(
            "SELECT MAX(global_number) AS max_global FROM heats WHERE tournament_id = ? AND day = ?",
            (tournament_id, pending_tie_break["day"]),
        ).fetchone()["max_global"]

    all_heats = list(staging_heats) + championship["wildcard"]["heats"] + championship["preliminary"]["heats"]
    if championship["final"]["heat"] is not None:
        all_heats.append(championship["final"]["heat"])
    apply_heat_locks(all_heats, tie_locked_after_global=tie_lock_boundary)

    days = []
    for day in range(1, tournament["days"] + 1):
        day_heats = sorted(
            (heat for heat in staging_heats if heat["day"] == day),
            key=lambda heat: heat["heatNumber"],
        )
        days.append({"day": day, "heats": day_heats})

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
            "wildcardMaxMarblesPerHeat": tournament["wildcard_max_marbles_per_heat"],
            "preliminaryMaxMarblesPerHeat": tournament["preliminary_max_marbles_per_heat"],
            "maxFinalByeMarblesPerRacer": tournament["max_final_bye_marbles_per_racer"],
            "maxPrelimMarblesForRacerWithFinalBye": tournament[
                "max_prelim_marbles_for_racer_with_final_bye"
            ],
            "maxWildcardMarblesForRacerWithFinalBye": tournament[
                "max_wildcard_marbles_for_racer_with_final_bye"
            ],
            "allowCascadingFinalByeSelection": bool(
                tournament["allow_cascading_final_bye_selection"]
            ),
            "maxPrelimPromotionMarblesPerRacer": tournament[
                "max_prelim_promotion_marbles_per_racer"
            ],
            "allowCascadingPrelimPromotionSelection": bool(
                tournament["allow_cascading_prelim_promotion_selection"]
            ),
            "maxWildcardMarblesForRacerWithPrelimPromotion": tournament[
                "max_wildcard_marbles_for_racer_with_prelim_promotion"
            ],
            "maxWildcardPromotionMarblesPerRacer": tournament[
                "max_wildcard_promotion_marbles_per_racer"
            ],
            "allowCascadingWildcardPromotionSelection": bool(
                tournament["allow_cascading_wildcard_promotion_selection"]
            ),
            "wildcardRacersPromotedPerHeat": tournament["wildcard_racers_promoted_per_heat"],
            "preliminaryRacersPromotedPerHeat": tournament["preliminary_racers_promoted_per_heat"],
            "maxFinalRacers": tournament["max_final_racers"],
            "totalHeats": total_heats,
            "completedHeats": completed_heats,
            "liveRoundDay": find_in_progress_staging_day(connection, tournament_id),
        },
        "tournaments": tournament_summaries(connection),
        "contestants": contestants,
        "points": points,
        "days": days,
        "standings": table,
        "championship": championship,
        "pendingTieBreak": pending_tie_break,
    }


@app.get("/api/state")
def get_state():
    connection = connect()
    try:
        tournament_id = resolve_tournament_id(connection)
        return jsonify(build_state(connection, tournament_id))
    finally:
        connection.close()


def state_event(payload: dict[str, Any], retry: int | None = None) -> str:
    prefix = f"retry: {retry}\n" if retry is not None else ""
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"{prefix}event: state\ndata: {encoded}\n\n"


@app.get("/api/tournaments/<int:tournament_id>/events")
def tournament_events(tournament_id: int):
    # Resolve before starting the response so a missing tournament returns a
    # normal JSON 404 instead of failing inside an established event stream.
    connection = connect()
    try:
        tournament_id = resolve_tournament_id(connection, tournament_id)
    finally:
        connection.close()

    def stream():
        stream_connection = connect()
        last_payload = ""
        last_data_version = -1
        last_keepalive = time.monotonic()
        first_event = True
        try:
            while True:
                data_version = stream_connection.execute("PRAGMA data_version").fetchone()[0]
                if first_event or data_version != last_data_version:
                    exists = stream_connection.execute(
                        "SELECT 1 FROM tournaments WHERE id = ?", (tournament_id,)
                    ).fetchone()
                    if not exists:
                        yield "event: tournament-deleted\ndata: {}\n\n"
                        return
                    payload = build_state(stream_connection, tournament_id)
                    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                    if first_event or encoded != last_payload:
                        yield state_event(payload, retry=2000 if first_event else None)
                        last_payload = encoded
                        last_keepalive = time.monotonic()
                    last_data_version = data_version
                    first_event = False
                if time.monotonic() - last_keepalive >= 15:
                    yield ": keepalive\n\n"
                    last_keepalive = time.monotonic()
                time.sleep(1)
        except GeneratorExit:
            return
        finally:
            stream_connection.close()

    return Response(
        stream_with_context(stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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


def boolean_field(data: dict[str, Any], name: str, default: bool) -> bool:
    value = data.get(name)
    if value is None:
        return default
    return bool(value)


def validated_tournament_name(value: Any) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 80:
        raise ApiError("Tournament name must be between 1 and 80 characters.")
    return name


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
        "wildcardRacersPromotedPerHeat": data.get("wildcardRacersPromotedPerHeat", 2),
        "preliminaryRacersPromotedPerHeat": data.get("preliminaryRacersPromotedPerHeat", 2),
        "maxFinalByeMarblesPerRacer": data.get("maxFinalByeMarblesPerRacer", 2),
        "maxPrelimMarblesForRacerWithFinalBye": data.get(
            "maxPrelimMarblesForRacerWithFinalBye", 0
        ),
        "maxWildcardMarblesForRacerWithFinalBye": data.get(
            "maxWildcardMarblesForRacerWithFinalBye", 0
        ),
        "maxPrelimPromotionMarblesPerRacer": data.get(
            "maxPrelimPromotionMarblesPerRacer", 1
        ),
        "maxWildcardMarblesForRacerWithPrelimPromotion": data.get(
            "maxWildcardMarblesForRacerWithPrelimPromotion", 0
        ),
        "maxWildcardPromotionMarblesPerRacer": data.get(
            "maxWildcardPromotionMarblesPerRacer", 2
        ),
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
    try:
        racers_per_heat = calculate_racers_per_heat(
            len(contestants),
            heats_per_racer_per_day,
            max_marbles_per_heat,
            marbles_per_racer,
        )
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    wildcard_max_marbles_per_heat = integer_field(
        data, "wildcardMaxMarblesPerHeat", 2, MAX_MARBLES_PER_HEAT, "Max marbles per wildcard heat"
    )
    preliminary_max_marbles_per_heat = integer_field(
        data, "preliminaryMaxMarblesPerHeat", 2, MAX_MARBLES_PER_HEAT, "Max marbles per preliminary heat"
    )
    max_final_bye_marbles_per_racer = integer_field(
        normalized_data, "maxFinalByeMarblesPerRacer", 0, 20, "Max final bye marbles per racer"
    )
    max_prelim_marbles_for_racer_with_final_bye = integer_field(
        normalized_data,
        "maxPrelimMarblesForRacerWithFinalBye",
        0,
        20,
        "Max prelim marbles for racer with final bye",
    )
    max_wildcard_marbles_for_racer_with_final_bye = integer_field(
        normalized_data,
        "maxWildcardMarblesForRacerWithFinalBye",
        0,
        20,
        "Max wildcard marbles for racer with final bye",
    )
    allow_cascading_final_bye_selection = boolean_field(
        data, "allowCascadingFinalByeSelection", True
    )
    max_prelim_promotion_marbles_per_racer = integer_field(
        normalized_data,
        "maxPrelimPromotionMarblesPerRacer",
        0,
        20,
        "Max prelim promotion marbles per racer",
    )
    allow_cascading_prelim_promotion_selection = boolean_field(
        data, "allowCascadingPrelimPromotionSelection", True
    )
    max_wildcard_marbles_for_racer_with_prelim_promotion = integer_field(
        normalized_data,
        "maxWildcardMarblesForRacerWithPrelimPromotion",
        0,
        20,
        "Max wildcard marbles for racer with prelim promotion",
    )
    max_wildcard_promotion_marbles_per_racer = integer_field(
        normalized_data,
        "maxWildcardPromotionMarblesPerRacer",
        0,
        20,
        "Max wildcard promotion marbles per racer",
    )
    allow_cascading_wildcard_promotion_selection = boolean_field(
        data, "allowCascadingWildcardPromotionSelection", True
    )
    wildcard_racers_promoted_per_heat = integer_field(
        normalized_data,
        "wildcardRacersPromotedPerHeat",
        1,
        24,
        "Wildcard racers promoted per heat",
    )
    preliminary_racers_promoted_per_heat = integer_field(
        normalized_data,
        "preliminaryRacersPromotedPerHeat",
        1,
        24,
        "Preliminary racers promoted per heat",
    )
    max_final_racers = integer_field(
        data, "maxFinalRacers", 2, min(24, len(contestants)), "Max final racers"
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
        "wildcardMaxMarblesPerHeat": wildcard_max_marbles_per_heat,
        "preliminaryMaxMarblesPerHeat": preliminary_max_marbles_per_heat,
        "maxFinalByeMarblesPerRacer": max_final_bye_marbles_per_racer,
        "maxPrelimMarblesForRacerWithFinalBye": max_prelim_marbles_for_racer_with_final_bye,
        "maxWildcardMarblesForRacerWithFinalBye": max_wildcard_marbles_for_racer_with_final_bye,
        "allowCascadingFinalByeSelection": allow_cascading_final_bye_selection,
        "maxPrelimPromotionMarblesPerRacer": max_prelim_promotion_marbles_per_racer,
        "allowCascadingPrelimPromotionSelection": allow_cascading_prelim_promotion_selection,
        "maxWildcardMarblesForRacerWithPrelimPromotion": (
            max_wildcard_marbles_for_racer_with_prelim_promotion
        ),
        "maxWildcardPromotionMarblesPerRacer": max_wildcard_promotion_marbles_per_racer,
        "allowCascadingWildcardPromotionSelection": allow_cascading_wildcard_promotion_selection,
        "wildcardRacersPromotedPerHeat": wildcard_racers_promoted_per_heat,
        "preliminaryRacersPromotedPerHeat": preliminary_racers_promoted_per_heat,
        "maxFinalRacers": max_final_racers,
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
            or len(current_racers) != len(data["contestants"])
            or current_points != data["points"]
        )
        championship_settings_changed = (
            current["wildcard_max_marbles_per_heat"] != data["wildcardMaxMarblesPerHeat"]
            or current["preliminary_max_marbles_per_heat"] != data["preliminaryMaxMarblesPerHeat"]
            or current["max_final_bye_marbles_per_racer"] != data["maxFinalByeMarblesPerRacer"]
            or current["max_prelim_marbles_for_racer_with_final_bye"]
            != data["maxPrelimMarblesForRacerWithFinalBye"]
            or current["max_wildcard_marbles_for_racer_with_final_bye"]
            != data["maxWildcardMarblesForRacerWithFinalBye"]
            or bool(current["allow_cascading_final_bye_selection"])
            != data["allowCascadingFinalByeSelection"]
            or current["max_prelim_promotion_marbles_per_racer"]
            != data["maxPrelimPromotionMarblesPerRacer"]
            or bool(current["allow_cascading_prelim_promotion_selection"])
            != data["allowCascadingPrelimPromotionSelection"]
            or current["max_wildcard_marbles_for_racer_with_prelim_promotion"]
            != data["maxWildcardMarblesForRacerWithPrelimPromotion"]
            or current["max_wildcard_promotion_marbles_per_racer"]
            != data["maxWildcardPromotionMarblesPerRacer"]
            or bool(current["allow_cascading_wildcard_promotion_selection"])
            != data["allowCascadingWildcardPromotionSelection"]
            or current["wildcard_racers_promoted_per_heat"]
            != data["wildcardRacersPromotedPerHeat"]
            or current["preliminary_racers_promoted_per_heat"]
            != data["preliminaryRacersPromotedPerHeat"]
            or current["max_final_racers"] != data["maxFinalRacers"]
        )
        has_results = connection.execute(
            """
            SELECT 1
            FROM heat_entries he
            JOIN heats h ON h.id = he.heat_id
            WHERE h.tournament_id = ? AND h.stage = 'staging' AND he.finish IS NOT NULL
            LIMIT 1
            """,
            (tournament_id,),
        ).fetchone() is not None
        has_championship_results = any(
            stage_has_results(connection, tournament_id, stage)
            for stage in ("wildcard", "preliminary", "final")
        )
        if structure_changed and has_results and not data["confirmReset"]:
            raise ApiError(
                "This change rebuilds this tournament's schedule and clears its heat results.",
                status=409,
                requiresReset=True,
            )
        if (
            not structure_changed
            and championship_settings_changed
            and has_championship_results
            and not data["confirmReset"]
        ):
            raise ApiError(
                "This change can rebuild this tournament's championship bracket and clear its results.",
                status=409,
                requiresReset=True,
            )

        connection.execute(
            """
            UPDATE tournaments
            SET name = ?, days = ?, heats_per_day = ?, heats_per_racer_per_day = ?,
                racers_per_heat = ?, max_marbles_per_heat = ?, marbles_per_racer = ?,
                wildcard_max_marbles_per_heat = ?, preliminary_max_marbles_per_heat = ?,
                max_final_bye_marbles_per_racer = ?,
                max_prelim_marbles_for_racer_with_final_bye = ?,
                max_wildcard_marbles_for_racer_with_final_bye = ?,
                allow_cascading_final_bye_selection = ?,
                max_prelim_promotion_marbles_per_racer = ?,
                allow_cascading_prelim_promotion_selection = ?,
                max_wildcard_marbles_for_racer_with_prelim_promotion = ?,
                max_wildcard_promotion_marbles_per_racer = ?,
                allow_cascading_wildcard_promotion_selection = ?,
                wildcard_racers_promoted_per_heat = ?, preliminary_racers_promoted_per_heat = ?,
                max_final_racers = ?,
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
                data["wildcardMaxMarblesPerHeat"],
                data["preliminaryMaxMarblesPerHeat"],
                data["maxFinalByeMarblesPerRacer"],
                data["maxPrelimMarblesForRacerWithFinalBye"],
                data["maxWildcardMarblesForRacerWithFinalBye"],
                data["allowCascadingFinalByeSelection"],
                data["maxPrelimPromotionMarblesPerRacer"],
                data["allowCascadingPrelimPromotionSelection"],
                data["maxWildcardMarblesForRacerWithPrelimPromotion"],
                data["maxWildcardPromotionMarblesPerRacer"],
                data["allowCascadingWildcardPromotionSelection"],
                data["wildcardRacersPromotedPerHeat"],
                data["preliminaryRacersPromotedPerHeat"],
                data["maxFinalRacers"],
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
        sync_championship(connection, tournament_id)
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
        heat_row = connection.execute(
            "SELECT tournament_id, stage, started_at, global_number FROM heats WHERE id = ?",
            (heat_id,),
        ).fetchone()
        entries = connection.execute(
            """
            SELECT h.tournament_id, he.racer_id, he.marble_number, he.finish AS existing_finish
            FROM heat_entries he
            JOIN heats h ON h.id = he.heat_id
            WHERE he.heat_id = ?
            ORDER BY he.lane, he.marble_number
            """,
            (heat_id,),
        ).fetchall()
        if not heat_row or not entries:
            raise ApiError("Heat not found.", status=404)
        tournament_id = int(entries[0]["tournament_id"])
        stage = heat_row["stage"]
        if heat_row["started_at"] is None:
            already_scored = any(row["existing_finish"] is not None for row in entries)
            if not already_scored:
                raise ApiError("Start this heat before entering results.", status=409)
        if is_heat_edit_locked(connection, tournament_id, heat_row["global_number"]):
            raise ApiError(
                "This heat is locked because a later heat has already started.",
                status=409,
            )
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
        if stage == "final" and 1 not in placed_finishes:
            raise ApiError("The final must have exactly one first-place finisher.")
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
        sync_championship(connection, tournament_id)
        return jsonify(build_state(connection, tournament_id))


@app.put("/api/tournaments/<int:tournament_id>/staging/<int:day>/tiebreak")
def resolve_round_tiebreak(tournament_id: int, day: int):
    payload = request.get_json(silent=True) or {}
    order = payload.get("order")
    if not isinstance(order, list) or not order:
        raise ApiError("Supply the tied racers in the order they should rank.")
    try:
        racer_ids = [int(racer_id) for racer_id in order]
    except (TypeError, ValueError) as exc:
        raise ApiError("Every entry in the order must be a racer id.") from exc
    if len(set(racer_ids)) != len(racer_ids):
        raise ApiError("Each tied racer can only appear once in the order.")
    with transaction() as connection:
        tournament_id = resolve_tournament_id(connection, tournament_id)
        pending = pending_round_tiebreak(connection, tournament_id)
        if pending is None or pending["day"] != day:
            raise ApiError("There is no pending tie to resolve for this round.", status=409)
        if set(racer_ids) != {racer["id"] for racer in pending["racers"]}:
            raise ApiError("The submitted racers do not match the tied racers for this round.")
        connection.execute(
            "DELETE FROM round_tiebreaks WHERE tournament_id = ? AND day = ?",
            (tournament_id, day),
        )
        connection.executemany(
            "INSERT INTO round_tiebreaks (tournament_id, day, racer_id, resolved_rank) VALUES (?, ?, ?, ?)",
            [(tournament_id, day, racer_id, rank) for rank, racer_id in enumerate(racer_ids)],
        )
        return jsonify(build_state(connection, tournament_id))


@app.put("/api/heats/<int:heat_id>/start")
def start_heat(heat_id: int):
    with transaction() as connection:
        heat_row = connection.execute(
            "SELECT tournament_id, stage, global_number, started_at FROM heats WHERE id = ?",
            (heat_id,),
        ).fetchone()
        if not heat_row:
            raise ApiError("Heat not found.", status=404)
        tournament_id = int(heat_row["tournament_id"])
        if heat_row["started_at"] is not None:
            return jsonify(build_state(connection, tournament_id))
        if is_heat_locked(connection, tournament_id, heat_row["global_number"]):
            raise ApiError(
                "Complete the earlier rounds before starting this heat.", status=409
            )
        connection.execute(
            "UPDATE heats SET started_at = CURRENT_TIMESTAMP WHERE id = ?", (heat_id,)
        )
        return jsonify(build_state(connection, tournament_id))


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
    app.run(host="0.0.0.0", port=7272, debug=False)
