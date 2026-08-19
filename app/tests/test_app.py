from __future__ import annotations

import os
import tempfile
import unittest
from collections import Counter
from itertools import combinations


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["APP_DATA_DIR"] = TEST_DATA.name

from app import app  # noqa: E402
from db import (  # noqa: E402
    balanced_schedule,
    championship_field,
    connect,
    heat_top_n,
    preliminary_field,
    standings,
    wildcard_field,
)
from report import final_finish_label, heat_finish_label  # noqa: E402


def start_heat(client, heat):
    return client.put(f'/api/heats/{heat["id"]}/start')


def build_sequential_results(heat):
    results = []
    position = 0
    for entry in heat["entries"]:
        for race_marble in entry["marbles"]:
            position += 1
            results.append(
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": race_marble["number"],
                    "finish": position,
                }
            )
    return results


def score_heat_sequentially(client, heat):
    start_heat(client, heat)
    results = build_sequential_results(heat)
    return client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})


def score_all_heats_sequentially(client, heats):
    state = None
    for heat in heats:
        response = score_heat_sequentially(client, heat)
        assert response.status_code == 200, response.get_json()
        state = response.get_json()
    return state


class MarbleRaceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        client = app.test_client()
        response = client.get("/api/tournaments")
        cls.initial_tournaments = response.get_json()["tournaments"]
        response.close()
        created = client.post(
            "/api/tournaments", json={"name": "The Great Marble Race"}
        )
        if created.status_code != 201:
            raise RuntimeError("Could not create the shared tournament test fixture.")
        created.close()

    @classmethod
    def tearDownClass(cls) -> None:
        TEST_DATA.cleanup()

    def setUp(self) -> None:
        self.client = app.test_client()

    def test_seeded_state_has_schedule_and_standings(self) -> None:
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state["competition"]["name"], "The Great Marble Race")
        self.assertEqual(state["competition"]["id"], 1)
        self.assertEqual(len(state["tournaments"]), 1)
        self.assertEqual(len(state["days"]), 3)
        self.assertEqual(state["competition"]["heatsPerRacerPerDay"], 3)
        self.assertEqual(state["competition"]["maxMarblesPerHeat"], 6)
        self.assertEqual(state["competition"]["wildcardRacersPromotedPerHeat"], 2)
        self.assertEqual(state["competition"]["preliminaryRacersPromotedPerHeat"], 2)
        self.assertEqual(state["competition"]["racersPerHeat"], 6)
        self.assertEqual(state["competition"]["marblesPerHeat"], 6)
        self.assertEqual(state["competition"]["marblesPerRacer"], 1)
        self.assertEqual(len(state["days"][0]["heats"]), 4)
        self.assertTrue(
            all(
                len(entry["marbles"]) == 1
                for entry in state["days"][0]["heats"][0]["entries"]
            )
        )
        self.assertEqual(len(state["standings"]), 8)
        for stage in ("wildcard", "preliminary"):
            self.assertFalse(state["championship"][stage]["ready"])
            self.assertEqual(state["championship"][stage]["heats"], [])
        self.assertFalse(state["championship"]["final"]["ready"])
        self.assertIsNone(state["championship"]["final"]["heat"])

    def test_new_database_starts_without_a_tournament(self) -> None:
        self.assertEqual(self.initial_tournaments, [])

    def test_rollrank_landing_and_workspace_routes(self) -> None:
        landing = self.client.get("/")
        workspace = self.client.get("/workspace")
        self.assertEqual(landing.status_code, 200)
        self.assertEqual(workspace.status_code, 200)
        html = landing.get_data(as_text=True)
        self.assertIn("RollRank", html)
        self.assertIn("Tournament hub", html)
        self.assertIn('id="tournament-index-list"', html)
        self.assertIn("+ New tournament", html)
        self.assertNotIn("Run the race.", html)
        self.assertNotIn('/static/rollrank-hero.png', html)
        self.assertIn('id="workspace-shell" hidden', html)
        landing.close()
        workspace.close()

    def test_schedule_balances_appearances_and_opponents(self) -> None:
        state = self.client.get("/api/state").get_json()
        target = state["competition"]["heatsPerRacerPerDay"]
        pair_counts = Counter()
        for day in state["days"]:
            appearances = Counter()
            for heat in day["heats"]:
                racer_ids = [entry["contestantId"] for entry in heat["entries"]]
                appearances.update(racer_ids)
                pair_counts.update(tuple(sorted(pair)) for pair in combinations(racer_ids, 2))
            self.assertTrue(all(value == target for value in appearances.values()))
            self.assertEqual(len(appearances), len(state["contestants"]))
        self.assertLessEqual(max(pair_counts.values()) - min(pair_counts.values()), 2)

    def test_schedule_reaches_even_pairing_when_configuration_allows_it(self) -> None:
        schedule = balanced_schedule(list(range(1, 8)), 4, 4, 3, 42)
        pair_counts = Counter()
        for day in schedule:
            appearances = Counter(racer_id for heat in day for racer_id in heat)
            self.assertEqual([appearances[racer_id] for racer_id in range(1, 8)], [4] * 7)
            for heat in day:
                pair_counts.update(tuple(sorted(pair)) for pair in combinations(heat, 2))
        self.assertEqual(
            {pair_counts[pair] for pair in combinations(range(1, 8), 2)},
            {6},
        )

    def test_complete_heat_updates_points(self) -> None:
        state = self.client.get("/api/state").get_json()
        heat = state["days"][0]["heats"][0]
        results = []
        position = 0
        for entry in heat["entries"]:
            for race_marble in entry["marbles"]:
                position += 1
                results.append(
                    {
                        "contestantId": entry["contestantId"],
                        "marbleNumber": race_marble["number"],
                        "finish": position,
                    }
                )
        start_heat(self.client, heat)
        response = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
        self.assertEqual(response.status_code, 200)
        updated = response.get_json()
        self.assertEqual(updated["competition"]["completedHeats"], 1)
        self.assertTrue(updated["days"][0]["heats"][0]["complete"])
        summaries = self.client.get("/api/tournaments").get_json()["tournaments"]
        self.assertEqual(len(summaries), 1)
        self.assertIsNotNone(summaries[0]["leader"])
        self.assertEqual(
            summaries[0]["leader"]["wins"],
            max(racer["wins"] for racer in updated["standings"]),
        )

    def test_heat_dnf_scores_zero_and_completes_heat(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "DNF Test Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        heat = created["days"][0]["heats"][0]
        results = []
        next_finish = 1
        for index, entry in enumerate(heat["entries"]):
            for race_marble in entry["marbles"]:
                finish = 0 if index < 2 else next_finish
                if finish:
                    next_finish += 1
                results.append(
                    {
                        "contestantId": entry["contestantId"],
                        "marbleNumber": race_marble["number"],
                        "finish": finish,
                    }
                )

        start_heat(self.client, heat)
        response = self.client.put(
            f'/api/heats/{heat["id"]}/results', json={"results": results}
        )
        self.assertEqual(response.status_code, 200)
        updated = response.get_json()
        updated_heat = next(
            item
            for day in updated["days"]
            for item in day["heats"]
            if item["id"] == heat["id"]
        )
        self.assertTrue(updated_heat["complete"])
        dnf_marbles = [
            race_marble
            for entry in updated_heat["entries"]
            for race_marble in entry["marbles"]
            if race_marble["finish"] == 0
        ]
        self.assertEqual(len(dnf_marbles), 2)
        self.assertTrue(all(race_marble["points"] == 0 for race_marble in dnf_marbles))
        self.assertEqual(heat_finish_label(0), "DNF")
        self.assertEqual(heat_finish_label(2), "2nd")

    def test_pdf_endpoint(self) -> None:
        tournament_id = self.client.get("/api/state").get_json()["competition"]["id"]
        response = self.client.get(f"/api/tournaments/{tournament_id}/report.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))
        self.assertGreater(len(response.data), 5000)

    def test_final_labels_and_podium_are_in_frontend(self) -> None:
        index_response = self.client.get("/")
        frontend_response = self.client.get("/static/app.js")
        styles_response = self.client.get("/static/styles.css")
        index = index_response.get_data(as_text=True)
        frontend = frontend_response.get_data(as_text=True)
        styles = styles_response.get_data(as_text=True)
        index_response.close()
        frontend_response.close()
        styles_response.close()
        self.assertIn('<button data-view="heats"><span class="nav-icon" aria-hidden="true">🏁</span><span>Rounds</span></button>', index)
        self.assertNotIn('data-view="championship"', index)
        self.assertIn('rel="icon" type="image/png" href="/static/marble-logo.png"', index)
        self.assertIn('class="brand-mark" src="/static/marble-logo.png"', index)
        self.assertIn('class="brand-wordmark"><strong>RollRank</strong>', index)
        self.assertIn('RollRank · Marble tournament control', index)
        self.assertIn('class="pdf-icon" aria-hidden="true"></span>', frontend)
        self.assertIn('/static/fontawesome-file-pdf.svg', styles)
        self.assertNotIn("↗", index)
        self.assertIn('id="tournament-switcher"', index)
        self.assertNotIn('id="race-title"', index)
        self.assertNotIn("raceTitle", frontend)
        self.assertIn('document.title = `${state.competition.name} · RollRank`', frontend)
        self.assertIn("Live leader", frontend)
        self.assertIn("stat.live && !stat.liveAlreadyCounted ? stat.count + 1 : stat.count", frontend)
        self.assertNotIn("displayCount: stat.live ? stat.count + 1 : stat.count", frontend)
        self.assertIn("Open dashboard", frontend)
        self.assertIn("No tournaments yet", frontend)
        self.assertIn('class="new-tournament-label">New</span>', index)
        self.assertIn("Create a fresh tournament", index)
        self.assertIn("Max Racers in Final", frontend)
        self.assertIn("Max marbles in wildcard/prelim heats", frontend)
        self.assertIn("Max bye marbles per racer", frontend)
        self.assertIn("Wildcard racers promoted / heat", frontend)
        self.assertIn("Preliminary racers promoted / heat", frontend)
        self.assertIn('name="wildcardRacersPromotedPerHeat"', frontend)
        self.assertIn('name="preliminaryRacersPromotedPerHeat"', frontend)
        self.assertIn("Top 3 podium", frontend)
        self.assertIn("Gold trophy", frontend)
        self.assertIn("Silver trophy", frontend)
        self.assertIn("Bronze trophy", frontend)
        self.assertIn("Sad face", frontend)
        self.assertIn("mobile-standings", frontend)
        self.assertIn("const form = event.target;", frontend)
        self.assertNotIn("const form = event.currentTarget;", frontend)
        self.assertIn("Race rounds", frontend)
        self.assertIn("Heats per racer / round", frontend)
        self.assertIn("Marbles per racer / heat", frontend)
        self.assertIn("Max marbles per heat", frontend)
        self.assertNotIn("<span>Racers per heat</span>", frontend)
        self.assertIn("data-marble-number", frontend)
        self.assertIn("DNF · 0 pts", frontend)
        self.assertIn('viewHeader("Results desk", "Rounds"', frontend)
        self.assertIn('data-day="championship"', frontend)
        self.assertNotIn("Enter heat results", frontend)
        self.assertNotIn("Review completed heats", frontend)
        self.assertNotIn("Edit tournament", frontend)
        self.assertNotIn(">Print report</a>", frontend)
        self.assertIn("Tournament standings", frontend)
        self.assertNotIn("Championship standings", frontend)
        self.assertNotIn("Race days", frontend)
        self.assertNotIn("Heats per racer / day", frontend)
        self.assertIn("@media (max-width:370px)", styles)
        self.assertIn("grid-template-columns:repeat(4,1fr)", styles)
        self.assertIn("padding:10px 14px; backdrop-filter:none", styles)
        self.assertIn("top:auto; right:0; bottom:0", styles)
        self.assertIn("align-self:auto", styles)
        self.assertIn("flex:0 0 34px", styles)
        self.assertIn("grid-template-columns:minmax(0,1fr) 42px", styles)
        self.assertIn('url("/static/rollrank-hero.png") center/cover fixed', styles)
        self.assertIn("background:rgba(5,21,34,.92)", styles)
        self.assertIn(".workspace-mode .view-heading h1 { color:#fff; }", styles)
        self.assertIn("data-enter-kiosk", frontend)
        self.assertIn("data-exit-kiosk", frontend)
        self.assertIn("function renderKioskDashboard()", frontend)
        self.assertIn("window.setInterval(refreshLiveState, 5000)", frontend)
        self.assertIn('activeView === "dashboard" || activeView === "standings"', frontend)
        self.assertIn('url.searchParams.set("display", "kiosk")', frontend)
        self.assertIn(".workspace-mode.kiosk-mode", styles)
        self.assertIn(".kiosk-standing-list", styles)
        self.assertIn("function currentTournamentStatus()", frontend)
        self.assertIn('class="status-chip ${status.className}"', frontend)
        self.assertNotIn("Tournament in progress", frontend)
        self.assertIn(".status-chip.final-ready", styles)
        self.assertIn(".status-chip.complete", styles)
        self.assertIn("function renderKioskFinalDashboard()", frontend)
        self.assertIn('"Championship: Final"', frontend)
        self.assertIn("Championship: Wildcard Heat ${nextWildcard.heatNumber}", frontend)
        self.assertIn("Championship: Preliminary Heat ${nextPreliminary.heatNumber}", frontend)
        self.assertIn("function renderDashboardFinalSummary()", frontend)
        self.assertIn("if (state.championship.final.complete) return renderDashboardFinalSummary();", frontend)
        self.assertIn("function fireworksMarkup()", frontend)
        self.assertIn("if (championship.final.ready) return renderKioskFinalDashboard();", frontend)
        self.assertIn("dashboard-final-racers", frontend)
        self.assertIn("kiosk-final-racers", frontend)
        self.assertIn("kiosk-final-podium", frontend)
        self.assertIn("Awaiting result", frontend)
        self.assertIn("kiosk-fireworks", frontend)
        self.assertIn("function finalDnfPlace", frontend)
        self.assertIn("Number(racer.finish) > 0).length + 1", frontend)
        self.assertIn("finalFinishLabel", frontend)
        self.assertIn("dnfs.length", frontend)
        self.assertIn(".kiosk-final-dashboard", styles)
        self.assertIn(".dashboard-final-summary", styles)
        self.assertIn(".dashboard-final-racers", styles)
        self.assertIn("@keyframes kiosk-firework-burst", styles)
        self.assertIn(".kiosk-fireworks { display:none; }", styles)
        self.assertIn("no-cache", index_response.headers["Cache-Control"])
        # Championship bracket rewrite: three-stage rendering, no leftover
        # single-final editor.
        self.assertIn("function originBadge(entry)", frontend)
        self.assertIn(".origin-badge", styles)
        self.assertIn("championship-stage", frontend)
        self.assertNotIn("saveChampionship", frontend)
        self.assertNotIn("data-champ-result", frontend)
        self.assertNotIn("function finalResultOptions", frontend)

    def test_full_championship_workflow(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Full Bracket Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        staging_heats = [heat for day in created["days"] for heat in day["heats"]]
        state = score_all_heats_sequentially(self.client, staging_heats)
        self.assertTrue(state["championship"]["wildcard"]["ready"])

        if state["championship"]["wildcard"]["heats"]:
            state = score_all_heats_sequentially(self.client, state["championship"]["wildcard"]["heats"])
        self.assertTrue(state["championship"]["preliminary"]["ready"])

        if state["championship"]["preliminary"]["heats"]:
            state = score_all_heats_sequentially(self.client, state["championship"]["preliminary"]["heats"])
        self.assertTrue(state["championship"]["final"]["ready"])

        final_heat = state["championship"]["final"]["heat"]
        self.assertIsNotNone(final_heat)
        entries = final_heat["entries"]
        self.assertGreaterEqual(len(entries), 3)
        start_heat(self.client, final_heat)

        no_winner_results = [
            {
                "contestantId": entry["contestantId"],
                "marbleNumber": entry["marbles"][0]["number"],
                "finish": 0,
            }
            for entry in entries
        ]
        rejected = self.client.put(
            f'/api/heats/{final_heat["id"]}/results', json={"results": no_winner_results}
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("first-place", rejected.get_json()["error"])

        finisher_count = len(entries) - 2
        final_results = [
            {
                "contestantId": entry["contestantId"],
                "marbleNumber": entry["marbles"][0]["number"],
                "finish": index + 1 if index < finisher_count else 0,
            }
            for index, entry in enumerate(entries)
        ]
        response = self.client.put(
            f'/api/heats/{final_heat["id"]}/results', json={"results": final_results}
        )
        self.assertEqual(response.status_code, 200)
        finished = response.get_json()
        self.assertTrue(finished["championship"]["final"]["complete"])
        self.assertEqual(finished["championship"]["final"]["champion"]["finish"], 1)
        dnf_entries = [
            entry for entry in finished["championship"]["final"]["heat"]["entries"] if entry["finish"] == 0
        ]
        self.assertEqual(len(dnf_entries), 2)
        tied_last_place = finisher_count + 1
        tied_last_label = final_finish_label(0, tied_last_place)
        self.assertTrue(tied_last_label.startswith(f"T-{tied_last_place}"))
        self.assertTrue(tied_last_label.endswith(" DNF"))
        self.assertEqual(final_finish_label(0, 3), "T-3rd DNF")

    def test_multiple_marbles_per_racer_sum_heat_points(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Double Marble Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        racers = [
            {"name": racer["name"], "color": racer["color"]}
            for racer in created["contestants"][:6]
        ]
        response = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Double Marble Cup",
                "days": 1,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 6,
                "marblesPerRacer": 2,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 3,
                "points": [10, 7, 5, 3, 2, 1],
                "contestants": racers,
            },
        )
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state["competition"]["marblesPerRacer"], 2)
        self.assertEqual(state["competition"]["maxMarblesPerHeat"], 6)
        self.assertEqual(state["competition"]["racersPerHeat"], 3)
        self.assertEqual(state["competition"]["marblesPerHeat"], 6)
        self.assertEqual(len(state["points"]), 6)

        for heat_index, heat in enumerate(state["days"][0]["heats"]):
            self.assertTrue(all(len(entry["marbles"]) == 2 for entry in heat["entries"]))
            results = []
            finish = 0
            for entry in heat["entries"]:
                for race_marble in entry["marbles"]:
                    finish += 1
                    results.append(
                        {
                            "contestantId": entry["contestantId"],
                            "marbleNumber": race_marble["number"],
                            "finish": finish,
                        }
                    )
            start_heat(self.client, heat)
            scored = self.client.put(
                f'/api/heats/{heat["id"]}/results', json={"results": results}
            )
            self.assertEqual(scored.status_code, 200)
            state = scored.get_json()
            scored_heat = state["days"][0]["heats"][heat_index]
            self.assertEqual(
                [entry["points"] for entry in scored_heat["entries"]],
                [17, 8, 3],
            )
            connection = connect()
            try:
                top_two_racers = heat_top_n(connection, heat["id"], 2)
            finally:
                connection.close()
            self.assertEqual(
                top_two_racers,
                [heat["entries"][0]["contestantId"], heat["entries"][1]["contestantId"]],
            )

        self.assertTrue(state["championship"]["wildcard"]["ready"])
        self.assertEqual(
            [racer["dayPlacements"][0] for racer in state["standings"]],
            [1, 2, 3, 4, 5, 6],
        )
        self.assertEqual(state["standings"][0]["wins"], 1)
        self.assertEqual(sum(racer["wins"] for racer in state["standings"]), 1)
        # marblesPerRacer only applies to staging heats -- wildcard/preliminary
        # heats race one marble per qualifying occurrence (capped by
        # maxByeMarblesPerRacer, which is 1 here), regardless of this setting.
        wildcard_heats = state["championship"]["wildcard"]["heats"]
        self.assertTrue(wildcard_heats)
        self.assertTrue(
            all(len(entry["marbles"]) == 1 for heat in wildcard_heats for entry in heat["entries"])
        )

        # The final always races one marble per racer regardless of
        # marblesPerRacer, since champion/podium/DNF all key off a single
        # finish per racer.
        state = score_all_heats_sequentially(self.client, wildcard_heats)
        if state["championship"]["preliminary"]["heats"]:
            state = score_all_heats_sequentially(self.client, state["championship"]["preliminary"]["heats"])
        self.assertTrue(state["championship"]["final"]["ready"])
        final_heat = state["championship"]["final"]["heat"]
        self.assertTrue(all(len(entry["marbles"]) == 1 for entry in final_heat["entries"]))
        finished = score_heat_sequentially(self.client, final_heat).get_json()
        self.assertTrue(finished["championship"]["final"]["complete"])
        self.assertIsNotNone(finished["championship"]["final"]["champion"])
        self.assertEqual(finished["championship"]["final"]["champion"]["finish"], 1)

        deleted = self.client.delete(f"/api/tournaments/{tournament_id}")
        self.assertEqual(deleted.status_code, 200)

    def test_max_marbles_calculates_largest_complete_heat(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Calculated Heat Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        racers = [
            {"name": racer["name"], "color": racer["color"]}
            for racer in created["contestants"]
        ]
        response = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Calculated Heat Cup",
                "days": 1,
                "heatsPerRacerPerDay": 3,
                "maxMarblesPerHeat": 10,
                "marblesPerRacer": 2,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 6,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": racers,
            },
        )
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state["competition"]["racersPerHeat"], 4)
        self.assertEqual(state["competition"]["marblesPerHeat"], 8)
        self.assertEqual(state["competition"]["maxMarblesPerHeat"], 10)
        self.assertEqual(state["competition"]["heatsPerDay"], 6)
        self.assertTrue(
            all(
                len(heat["entries"]) == 4
                and sum(len(entry["marbles"]) for entry in heat["entries"]) == 8
                for heat in state["days"][0]["heats"]
            )
        )
        deleted = self.client.delete(f"/api/tournaments/{tournament_id}")
        self.assertEqual(deleted.status_code, 200)

    def test_tournaments_keep_settings_and_results_independent(self) -> None:
        original = self.client.get("/api/state").get_json()
        original_id = original["competition"]["id"]
        original_completed = original["competition"]["completedHeats"]
        response = self.client.post("/api/tournaments", json={"name": "Sprint Cup"})
        self.assertEqual(response.status_code, 201)
        created = response.get_json()
        second_id = created["competition"]["id"]
        self.assertNotEqual(second_id, original_id)
        self.assertEqual(len(created["tournaments"]), 2)

        racers = [
            {"name": racer["name"], "color": racer["color"]}
            for racer in created["contestants"][:6]
        ]
        configured = self.client.put(
            f"/api/tournaments/{second_id}",
            json={
                "name": "Sprint Cup",
                "days": 2,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 3,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 3,
                "points": [5, 3, 1],
                "contestants": racers,
            },
        )
        self.assertEqual(configured.status_code, 200)
        second = configured.get_json()
        self.assertEqual(second["competition"]["days"], 2)
        self.assertEqual(second["competition"]["totalHeats"], 4)
        heat = second["days"][0]["heats"][0]
        results = [
            {
                "contestantId": entry["contestantId"],
                "marbleNumber": entry["marbles"][0]["number"],
                "finish": position,
            }
            for position, entry in enumerate(heat["entries"], start=1)
        ]
        start_heat(self.client, heat)
        scored = self.client.put(
            f'/api/heats/{heat["id"]}/results', json={"results": results}
        ).get_json()
        self.assertEqual(scored["competition"]["id"], second_id)
        self.assertEqual(scored["competition"]["completedHeats"], 1)

        unchanged = self.client.get(f"/api/state?tournamentId={original_id}").get_json()
        self.assertEqual(unchanged["competition"]["name"], original["competition"]["name"])
        self.assertEqual(unchanged["competition"]["days"], original["competition"]["days"])
        self.assertEqual(unchanged["competition"]["completedHeats"], original_completed)

        deleted = self.client.delete(f"/api/tournaments/{second_id}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/state?tournamentId={second_id}").status_code,
            404,
        )

    def test_round_scoped_standings_and_bye_cap_forfeits_excess_wins(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Round Standings Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        contestant_ids = [racer["id"] for racer in created["contestants"]]
        configured = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Round Standings Cup",
                "days": 3,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 6,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()
        self.assertEqual(configured["competition"]["heatsPerDay"], 1)

        # racer[0] wins every round; everyone else rotates through the
        # remaining places so a different racer holds each other rank each day.
        state = configured
        for day_index, day in enumerate(state["days"]):
            heat = day["heats"][0]
            results = []
            for entry in heat["entries"]:
                index = contestant_ids.index(entry["contestantId"])
                finish = 1 if index == 0 else 2 + ((index - 1 + day_index) % 7)
                results.append(
                    {
                        "contestantId": entry["contestantId"],
                        "marbleNumber": entry["marbles"][0]["number"],
                        "finish": finish,
                    }
                )
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        connection = connect()
        try:
            field = championship_field(connection, tournament_id)
        finally:
            connection.close()

        winner_id = contestant_ids[0]
        winner_byes = [item for item in field["byes"] if item["racerId"] == winner_id]
        self.assertEqual(len(winner_byes), 1)
        self.assertEqual(winner_byes[0]["originRound"], 1)
        self.assertTrue(all(item["racerId"] != winner_id for item in field["preliminaryDirect"]))
        self.assertTrue(all(item["racerId"] != winner_id for item in field["wildcardPool"]))

    def test_championship_field_ignores_unraced_days(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Partial Rounds Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        configured = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Partial Rounds Cup",
                "days": 3,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 6,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()
        self.assertEqual(configured["competition"]["heatsPerDay"], 1)

        # Only day 1 gets raced; days 2 and 3 are untouched, so round_standings()
        # would rank them purely by sort_order if championship_field() didn't
        # skip them -- that phantom ranking must not claim bye/preliminary/
        # wildcard seats that belong to day 1's real result.
        day1_heat = configured["days"][0]["heats"][0]
        results = [
            {
                "contestantId": entry["contestantId"],
                "marbleNumber": entry["marbles"][0]["number"],
                "finish": index + 1,
            }
            for index, entry in enumerate(day1_heat["entries"])
        ]
        start_heat(self.client, day1_heat)
        saved = self.client.put(
            f'/api/heats/{day1_heat["id"]}/results', json={"results": results}
        )
        self.assertEqual(saved.status_code, 200)

        connection = connect()
        try:
            field = championship_field(connection, tournament_id)
        finally:
            connection.close()

        self.assertTrue(all(item["originRound"] == 1 for item in field["byes"]))
        self.assertTrue(all(item["originRound"] == 1 for item in field["preliminaryDirect"]))
        self.assertTrue(all(item["originRound"] == 1 for item in field["wildcardPool"]))

        third_place_id = day1_heat["entries"][2]["contestantId"]
        fourth_place_id = day1_heat["entries"][3]["contestantId"]
        wildcard_ids = {item["racerId"] for item in field["wildcardPool"]}
        self.assertEqual(wildcard_ids, {third_place_id, fourth_place_id})

    def test_standings_show_provisional_placements_until_the_round_fully_completes(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Pending Round Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        day1_heats = created["days"][0]["heats"]
        self.assertEqual(len(day1_heats), 4)
        self.assertIsNone(created["competition"]["liveRoundDay"])

        # Scoring just the first of day 1's four heats gives everyone a
        # provisional (not yet official) round placement, since the round
        # itself is still in progress -- but nobody's win/promotion tallies
        # move until the round actually finishes.
        partial = score_heat_sequentially(self.client, day1_heats[0]).get_json()
        self.assertEqual(partial["competition"]["liveRoundDay"], 1)
        self.assertTrue(all(racer["dayPlacements"][0] is not None for racer in partial["standings"]))
        self.assertTrue(all(racer["wins"] == 0 for racer in partial["standings"]))
        self.assertTrue(all(racer["preliminaryPromotions"] == 0 for racer in partial["standings"]))
        self.assertTrue(all(racer["wildcardAdvancements"] == 0 for racer in partial["standings"]))
        leader = next(racer for racer in partial["standings"] if racer["dayPlacements"][0] == 1)
        self.assertEqual(leader["dayChampionshipTiers"][0], "bye")
        self.assertTrue(leader["liveRoundLeader"])

        state = score_all_heats_sequentially(self.client, day1_heats[1:])
        self.assertIsNone(state["competition"]["liveRoundDay"])
        day1_placements = sorted(racer["dayPlacements"][0] for racer in state["standings"])
        self.assertEqual(day1_placements, list(range(1, 9)))
        self.assertTrue(any(racer["dayChampionshipTiers"][0] == "bye" for racer in state["standings"]))
        self.assertTrue(any(racer["wins"] == 1 for racer in state["standings"]))

    def test_live_round_preview_flags_the_provisional_leader_and_tiers(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Live Preview Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        day1_heat = created["days"][0]["heats"][0]

        # Nobody has raced yet -- no round is in progress, so nothing is
        # flagged as a provisional leader or tier.
        self.assertTrue(all(not racer["liveRoundLeader"] for racer in created["standings"]))
        self.assertTrue(all(racer["liveTier"] is None for racer in created["standings"]))

        partial = score_heat_sequentially(self.client, day1_heat).get_json()
        by_place = {
            entry["contestantId"]: index + 1
            for index, entry in enumerate(day1_heat["entries"])
        }
        standings_by_id = {racer["id"]: racer for racer in partial["standings"]}
        first_place_id = next(cid for cid, place in by_place.items() if place == 1)
        second_place_id = next(cid for cid, place in by_place.items() if place == 2)
        third_place_id = next(cid for cid, place in by_place.items() if place == 3)
        fourth_place_id = next(cid for cid, place in by_place.items() if place == 4)

        self.assertTrue(standings_by_id[first_place_id]["liveRoundLeader"])
        self.assertEqual(standings_by_id[first_place_id]["liveTier"], "bye")
        self.assertFalse(standings_by_id[second_place_id]["liveRoundLeader"])
        self.assertEqual(standings_by_id[second_place_id]["liveTier"], "preliminary")
        self.assertEqual(standings_by_id[third_place_id]["liveTier"], "wildcard")
        self.assertEqual(standings_by_id[fourth_place_id]["liveTier"], "wildcard")
        # Their round win/tier hasn't been finalized, so it isn't counted yet.
        self.assertEqual(standings_by_id[first_place_id]["wins"], 0)

        # Once the round fully completes, there's no round in progress
        # anymore, so nobody is flagged as a live leader or tier.
        day1_heats = created["days"][0]["heats"]
        finished = score_all_heats_sequentially(self.client, day1_heats[1:])
        self.assertTrue(all(not racer["liveRoundLeader"] for racer in finished["standings"]))
        self.assertTrue(all(racer["liveTier"] is None for racer in finished["standings"]))

    def test_wildcard_seats_reach_past_excluded_finishers_and_stay_uncapped(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Backfill Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        contestant_ids = [racer["id"] for racer in created["contestants"]]
        configured = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Backfill Cup",
                "days": 3,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 6,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()

        # Contestants are seeded as Ruby Rocket(0), Blue Bolt(1), Golden
        # Globe(2), Emerald Flash(3), Purple Comet(4), Orange Orbit(5),
        # Silver Streak(6), Pink Lightning(7). Day 1's 3rd/4th (Orange
        # Orbit, Pink Lightning) both go on to place 2nd/1st in a later
        # round, and day 3's 3rd/4th (Blue Bolt, Golden Globe) are likewise
        # already claimed elsewhere -- those rounds' wildcard seats reach
        # past them to the next eligible finisher rather than sitting empty.
        finish_orders = [
            [6, 1, 5, 7, 4, 2, 0, 3],
            [7, 3, 0, 2, 5, 4, 1, 6],
            [6, 5, 1, 2, 3, 0, 4, 7],
        ]
        state = configured
        for day, order in zip(state["days"], finish_orders):
            heat = day["heats"][0]
            finishes = {
                contestant_ids[racer_index]: place
                for place, racer_index in enumerate(order, start=1)
            }
            results = [
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": entry["marbles"][0]["number"],
                    "finish": finishes[entry["contestantId"]],
                }
                for entry in heat["entries"]
            ]
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        connection = connect()
        try:
            field = championship_field(connection, tournament_id)
        finally:
            connection.close()

        wildcard_days_by_racer: dict[int, list[int]] = {}
        for item in field["wildcardPool"]:
            wildcard_days_by_racer.setdefault(item["racerId"], []).append(item["originRound"])
        ruby_rocket, golden_globe, purple_comet = (contestant_ids[i] for i in (0, 2, 4))
        orange_orbit, pink_lightning, blue_bolt = (contestant_ids[i] for i in (5, 7, 1))

        # Naturally-qualifying 3rd/4th finishers who already claimed a
        # better tier elsewhere never show up in the wildcard pool.
        self.assertNotIn(orange_orbit, wildcard_days_by_racer)
        self.assertNotIn(pink_lightning, wildcard_days_by_racer)
        self.assertNotIn(blue_bolt, wildcard_days_by_racer)

        # Day 1's two seats (3rd/4th both claimed elsewhere) reach past them
        # to the next eligible finishers: Purple Comet (5th) and Golden
        # Globe (6th).
        self.assertEqual(sorted(wildcard_days_by_racer[purple_comet]), [1])
        # Golden Globe is eligible (never bye- or preliminary-tier) every
        # single round -- and unlike bye/preliminary, wildcard marbles are
        # uncapped, so they earn a marble in all three rounds, not just one.
        self.assertEqual(sorted(wildcard_days_by_racer[golden_globe]), [1, 2, 3])
        # Ruby Rocket is likewise eligible on both day 2 and day 3.
        self.assertEqual(sorted(wildcard_days_by_racer[ruby_rocket]), [2, 3])
        self.assertEqual(set(wildcard_days_by_racer), {purple_comet, golden_globe, ruby_rocket})

        connection = connect()
        try:
            table = standings(connection, tournament_id)
        finally:
            connection.close()

        by_id = {row["id"]: row for row in table}
        self.assertEqual(by_id[purple_comet]["dayChampionshipTiers"][0], "wildcard")
        self.assertEqual(by_id[golden_globe]["dayChampionshipTiers"], ["wildcard", "wildcard", "wildcard"])
        self.assertEqual(by_id[ruby_rocket]["dayChampionshipTiers"][1], "wildcard")
        self.assertEqual(by_id[ruby_rocket]["dayChampionshipTiers"][2], "wildcard")
        self.assertEqual(by_id[orange_orbit]["dayChampionshipTiers"][0], None)
        self.assertIsNotNone(by_id[orange_orbit]["dayChampionshipTiers"][2])

    def test_bye_ineligibility_cascades_preliminary_and_stacks_multiple_marbles(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Multi-Marble Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        contestant_ids = [racer["id"] for racer in created["contestants"]]
        state = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Multi-Marble Cup",
                "days": 3,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 24,
                "maxByeMarblesPerRacer": 2,
                "maxFinalRacers": 8,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()

        # Contestants seeded as Ruby Rocket(0)=A, Blue Bolt(1)=B, Golden
        # Globe(2)=C, Emerald Flash(3)=D, Purple Comet(4)=E, Orange
        # Orbit(5)=F, Silver Streak(6)=G, Pink Lightning(7)=H.
        #
        # A wins day 1 and day 2 (bye-eligible each time, cap 2 keeps both);
        # D wins day 3. B places 2nd on day 1 and day 2 -- two separate
        # preliminary marbles. Day 3's 2nd place is A, who is bye-ineligible
        # for preliminary, so the cascade moves to 3rd place (C) instead.
        # E and F are the natural wildcard pair on both day 1 and day 3,
        # stacking two wildcard marbles each -- wildcard is uncapped, so
        # this is their full natural count, not a truncation; G and H are
        # the natural pair on day 2 only, one marble each.
        finish_orders = [
            [0, 1, 4, 5, 6, 7, 2, 3],
            [0, 1, 6, 7, 4, 5, 3, 2],
            [3, 0, 2, 4, 5, 6, 7, 1],
        ]
        for day, order in zip(state["days"], finish_orders):
            heat = day["heats"][0]
            finishes = {
                contestant_ids[racer_index]: place
                for place, racer_index in enumerate(order, start=1)
            }
            results = [
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": entry["marbles"][0]["number"],
                    "finish": finishes[entry["contestantId"]],
                }
                for entry in heat["entries"]
            ]
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        a, b, c, d, e, f, g, h = contestant_ids

        connection = connect()
        try:
            field = championship_field(connection, tournament_id)
            prelim_entries = preliminary_field(connection, tournament_id)
            wildcard_entries = wildcard_field(connection, tournament_id)
        finally:
            connection.close()

        # Rule 1: a bye winner is permanently ineligible for preliminary or
        # wildcard, even from a round they didn't win.
        self.assertTrue(all(item["racerId"] != a for item in field["preliminaryDirect"]))
        self.assertTrue(all(item["racerId"] != a for item in field["wildcardPool"]))

        # Rule 2: day 3's bye-ineligible 2nd place (A) cascades to 3rd (C).
        prelim_by_racer = {item["racerId"]: item["originRound"] for item in field["preliminaryDirect"]}
        self.assertEqual(prelim_by_racer.get(c), 3)
        self.assertNotIn(a, prelim_by_racer)

        # Rule 3: B, who placed 2nd twice, gets two consolidated preliminary
        # marbles instead of losing the second occurrence.
        prelim_by_id = {entry["racerId"]: entry for entry in prelim_entries}
        self.assertEqual(prelim_by_id[b]["marbleSlots"], 2)
        self.assertEqual(prelim_by_id[c]["marbleSlots"], 1)

        # Rule 4: E and F each naturally qualify for the wildcard pool on two
        # different rounds and stack two marbles -- wildcard is uncapped, so
        # nothing forces this down further; G and H only qualify once each.
        wildcard_by_id = {entry["racerId"]: entry for entry in wildcard_entries}
        self.assertEqual(wildcard_by_id[e]["marbleSlots"], 2)
        self.assertEqual(wildcard_by_id[f]["marbleSlots"], 2)
        self.assertEqual(wildcard_by_id[g]["marbleSlots"], 1)
        self.assertEqual(wildcard_by_id[h]["marbleSlots"], 1)

        # The consolidated entries feed straight into the actual wildcard
        # heat, so a multi-marble racer races every earned marble in one
        # heat rather than being split or colliding on marble_number.
        state = self.client.get(f"/api/state?tournamentId={tournament_id}").get_json()
        wildcard_heats = state["championship"]["wildcard"]["heats"]
        self.assertTrue(wildcard_heats)
        entries_by_racer = {
            entry["contestantId"]: entry for heat in wildcard_heats for entry in heat["entries"]
        }
        self.assertEqual(len(entries_by_racer[e]["marbles"]), 2)
        self.assertEqual(len(entries_by_racer[f]["marbles"]), 2)
        self.assertEqual(len(entries_by_racer[g]["marbles"]), 1)

    def test_standings_rank_by_tier_not_raw_placement_count(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Tier Ranking Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        contestant_ids = [racer["id"] for racer in created["contestants"]]
        state = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Tier Ranking Cup",
                "days": 3,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 24,
                "maxByeMarblesPerRacer": 2,
                "maxFinalRacers": 8,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()

        # Same shape as the bye-ineligibility cascade scenario: A wins days
        # 1-2, so day 3's raw 2nd place (also A) is bye-ineligible and its
        # preliminary slot cascades to C (raw 3rd on day 3). That gives C
        # one real preliminary promotion despite never placing 2nd, while E
        # racks up two raw 3rd/4th finishes that both land as wildcard (E is
        # never promoted to preliminary). Ranking by tier should put C
        # (1 preliminary promotion) above E (0 promotions, 2 wildcard),
        # even though a raw-placement count would rank E above C.
        finish_orders = [
            [0, 1, 4, 5, 6, 7, 2, 3],
            [0, 1, 6, 7, 4, 5, 3, 2],
            [3, 0, 2, 4, 5, 6, 7, 1],
        ]
        for day, order in zip(state["days"], finish_orders):
            heat = day["heats"][0]
            finishes = {
                contestant_ids[racer_index]: place
                for place, racer_index in enumerate(order, start=1)
            }
            results = [
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": entry["marbles"][0]["number"],
                    "finish": finishes[entry["contestantId"]],
                }
                for entry in heat["entries"]
            ]
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        a, b, c, d, e, f, g, h = contestant_ids
        standings_by_id = {racer["id"]: racer for racer in state["standings"]}

        self.assertEqual(standings_by_id[c]["preliminaryPromotions"], 1)
        self.assertEqual(standings_by_id[e]["preliminaryPromotions"], 0)
        self.assertEqual(standings_by_id[e]["wildcardAdvancements"], 2)
        self.assertLess(standings_by_id[c]["rank"], standings_by_id[e]["rank"])

        # Full priority order: wins, then preliminary promotions, then
        # wildcard advancements.
        ranks = {racer_id: standings_by_id[racer_id]["rank"] for racer_id in (a, d, b, c, e)}
        self.assertEqual(
            sorted(ranks, key=ranks.get),
            [a, d, b, c, e],
        )

    def test_wildcard_marbles_split_across_heats_when_too_many_for_one(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Wildcard Split Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        contestant_ids = [racer["id"] for racer in created["contestants"]]
        state = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Wildcard Split Cup",
                "days": 5,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 3,
                "maxByeMarblesPerRacer": 4,
                "maxFinalRacers": 8,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()

        # Ruby Rocket(0)/Blue Bolt(1)/Golden Globe(2) rotate the win, Emerald
        # Flash(3) is always 2nd (a steady single-marble preliminary
        # qualifier), and Orange Orbit(5)/Purple Comet(4) are always
        # eligible for wildcard and rack up a marble every round (uncapped)
        # -- far more than the tiny championshipMaxMarblesPerHeat of 3 can
        # hold from one racer alongside anyone else.
        finish_orders = [
            [0, 3, 5, 1, 2, 4, 6, 7],
            [1, 3, 5, 2, 0, 4, 6, 7],
            [2, 3, 5, 0, 1, 4, 6, 7],
            [0, 3, 5, 1, 2, 4, 6, 7],
            [1, 3, 5, 2, 0, 4, 6, 7],
        ]
        for day, order in zip(state["days"], finish_orders):
            heat = day["heats"][0]
            finishes = {
                contestant_ids[racer_index]: place
                for place, racer_index in enumerate(order, start=1)
            }
            results = [
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": entry["marbles"][0]["number"],
                    "finish": finishes[entry["contestantId"]],
                }
                for entry in heat["entries"]
            ]
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        orange_orbit, purple_comet = contestant_ids[5], contestant_ids[4]

        connection = connect()
        try:
            entries = wildcard_field(connection, tournament_id)
        finally:
            connection.close()
        marbles_by_racer = {entry["racerId"]: entry["marbleSlots"] for entry in entries}
        self.assertEqual(marbles_by_racer[orange_orbit], 5)
        self.assertEqual(marbles_by_racer[purple_comet], 5)

        wildcard_heats = state["championship"]["wildcard"]["heats"]
        self.assertGreater(len(wildcard_heats), 1)

        # Each heavily-qualified racer's marbles are spread across more than
        # one heat, and never more than once in the same heat (which would
        # collide on marble_number) -- but the total across all heats still
        # equals their full marble count.
        for racer_id in (orange_orbit, purple_comet):
            heats_with_racer = [
                heat for heat in wildcard_heats
                if any(entry["contestantId"] == racer_id for entry in heat["entries"])
            ]
            self.assertGreater(len(heats_with_racer), 1)
            total_marbles = sum(
                len(entry["marbles"])
                for heat in heats_with_racer
                for entry in heat["entries"]
                if entry["contestantId"] == racer_id
            )
            self.assertEqual(total_marbles, 5)

            # Every marble is tagged with the specific staging round that
            # seeded it, and split chunks each carry only their own rounds
            # (never the full set duplicated onto every heat) -- but the
            # union across all of the racer's heats covers every round they
            # qualified in.
            seed_rounds_by_heat = [
                entry["seedRounds"]
                for heat in heats_with_racer
                for entry in heat["entries"]
                if entry["contestantId"] == racer_id
            ]
            self.assertTrue(all(len(rounds) < 5 for rounds in seed_rounds_by_heat))
            self.assertEqual(set().union(*seed_rounds_by_heat), {1, 2, 3, 4, 5})

    def test_wildcard_preliminary_interleaving_avoids_same_round_clustering(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Interleave Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        contestant_ids = [racer["id"] for racer in created["contestants"]]
        state = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Interleave Cup",
                "days": 3,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 3,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 6,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()

        # racers[6]/[7] always take 1st/2nd; each round's 3rd/4th (the wildcard
        # pool) are two racers no other round places in the wildcard zone.
        finish_orders = [
            [6, 7, 0, 1, 2, 3, 4, 5],
            [6, 7, 2, 3, 0, 1, 4, 5],
            [6, 7, 4, 5, 0, 1, 2, 3],
        ]
        for day, order in zip(state["days"], finish_orders):
            heat = day["heats"][0]
            finishes = {
                contestant_ids[racer_index]: place
                for place, racer_index in enumerate(order, start=1)
            }
            results = [
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": entry["marbles"][0]["number"],
                    "finish": finishes[entry["contestantId"]],
                }
                for entry in heat["entries"]
            ]
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        wildcard = state["championship"]["wildcard"]
        self.assertTrue(wildcard["ready"])
        self.assertGreaterEqual(len(wildcard["heats"]), 2)
        for heat in wildcard["heats"]:
            origin_rounds = [entry["originRound"] for entry in heat["entries"]]
            self.assertEqual(len(origin_rounds), len(set(origin_rounds)))

    def test_ladder_projects_known_placements_before_a_stage_unlocks(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Projection Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        contestant_ids = [racer["id"] for racer in created["contestants"]]
        state = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Projection Cup",
                "days": 3,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 5,
                "maxByeMarblesPerRacer": 1,
                "wildcardRacersPromotedPerHeat": 2,
                "preliminaryRacersPromotedPerHeat": 1,
                "maxFinalRacers": 6,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()

        # racers[6]/[7] always take 1st/2nd, so racer[6] banks the only bye
        # (day 1, capped at 1) and racer[7] is the sole direct preliminary
        # qualifier (day 1). The remaining six racers fill two 3-racer
        # wildcard heats.
        finish_orders = [
            [6, 7, 0, 1, 2, 3, 4, 5],
            [6, 7, 2, 3, 0, 1, 4, 5],
            [6, 7, 4, 5, 0, 1, 2, 3],
        ]
        for day, order in zip(state["days"], finish_orders):
            heat = day["heats"][0]
            finishes = {
                contestant_ids[racer_index]: place
                for place, racer_index in enumerate(order, start=1)
            }
            results = [
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": entry["marbles"][0]["number"],
                    "finish": finishes[entry["contestantId"]],
                }
                for entry in heat["entries"]
            ]
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        champ = state["championship"]
        self.assertFalse(champ["preliminary"]["ready"])
        preliminary_projected = champ["preliminary"]["projectedEntries"]
        decided = [entry for entry in preliminary_projected if entry["decided"]]
        pending = [entry for entry in preliminary_projected if not entry["decided"]]
        self.assertEqual([entry["contestantId"] for entry in decided], [contestant_ids[7]])
        self.assertEqual(len(pending), 2 * len(champ["wildcard"]["heats"]))
        self.assertTrue(all(entry["originStage"] == "wildcard" for entry in pending))
        self.assertEqual({entry["qualifyingPlace"] for entry in pending}, {1, 2})

        self.assertFalse(champ["final"]["ready"])
        final_projected = champ["final"]["projectedEntries"]
        self.assertEqual(len(final_projected), 1)
        self.assertTrue(final_projected[0]["decided"])
        self.assertEqual(final_projected[0]["contestantId"], contestant_ids[6])
        self.assertEqual(final_projected[0]["originStage"], "bye")

        # Score just the first wildcard heat; its top two racers should now
        # appear in the preliminary projection while the other heat's two
        # slots are still pending.
        first_heat = champ["wildcard"]["heats"][0]
        second_heat_id = champ["wildcard"]["heats"][1]["id"]
        results = [
            {
                "contestantId": entry["contestantId"],
                "marbleNumber": entry["marbles"][0]["number"],
                "finish": index + 1,
            }
            for index, entry in enumerate(first_heat["entries"])
        ]
        start_heat(self.client, first_heat)
        saved = self.client.put(f'/api/heats/{first_heat["id"]}/results', json={"results": results})
        self.assertEqual(saved.status_code, 200)
        state = saved.get_json()

        self.assertFalse(state["championship"]["preliminary"]["ready"])
        projected = state["championship"]["preliminary"]["projectedEntries"]
        decided_ids = {entry["contestantId"] for entry in projected if entry["decided"]}
        self.assertIn(first_heat["entries"][0]["contestantId"], decided_ids)
        self.assertIn(first_heat["entries"][1]["contestantId"], decided_ids)
        still_pending = [entry for entry in projected if not entry["decided"]]
        self.assertEqual(len(still_pending), 2)
        self.assertTrue(all(entry["originHeatId"] == second_heat_id for entry in still_pending))

        second_heat = next(
            heat
            for heat in state["championship"]["wildcard"]["heats"]
            if heat["id"] == second_heat_id
        )
        saved = score_heat_sequentially(self.client, second_heat)
        self.assertEqual(saved.status_code, 200)
        state = saved.get_json()
        self.assertTrue(state["championship"]["preliminary"]["ready"])
        promoted_ids = {
            entry["contestantId"]
            for heat in state["championship"]["preliminary"]["heats"]
            for entry in heat["entries"]
            if entry.get("originStage") == "wildcard"
        }
        expected_promoted_ids = {
            entry["contestantId"]
            for heat in champ["wildcard"]["heats"]
            for entry in heat["entries"][:2]
        }
        self.assertEqual(promoted_ids, expected_promoted_ids)

        preliminary_heats = state["championship"]["preliminary"]["heats"]
        expected_finalists = {
            contestant_ids[6],
            *(heat["entries"][0]["contestantId"] for heat in preliminary_heats),
        }
        state = score_all_heats_sequentially(self.client, preliminary_heats)
        self.assertTrue(state["championship"]["final"]["ready"])
        actual_finalists = {
            entry["contestantId"] for entry in state["championship"]["final"]["heat"]["entries"]
        }
        self.assertEqual(actual_finalists, expected_finalists)

    def test_editing_an_earlier_staging_heat_is_blocked_once_later_heats_have_run(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Cascade Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        first_heat_id = created["days"][0]["heats"][0]["id"]
        staging_heats = [heat for day in created["days"] for heat in day["heats"]]
        state = score_all_heats_sequentially(self.client, staging_heats)
        self.assertTrue(state["championship"]["wildcard"]["ready"])
        self.assertTrue(state["championship"]["wildcard"]["heats"])
        state = score_all_heats_sequentially(self.client, state["championship"]["wildcard"]["heats"])
        self.assertTrue(state["championship"]["wildcard"]["complete"])

        # Every staging heat after the first one has since started, so the
        # first heat is permanently locked -- reversing its results is
        # rejected outright rather than offered as a confirmable cascade reset.
        heat = next(h for day in state["days"] for h in day["heats"] if h["id"] == first_heat_id)
        self.assertTrue(heat["editLocked"])
        entry_count = sum(len(entry["marbles"]) for entry in heat["entries"])
        position = entry_count
        reversed_results = []
        for entry in heat["entries"]:
            for race_marble in entry["marbles"]:
                reversed_results.append(
                    {
                        "contestantId": entry["contestantId"],
                        "marbleNumber": race_marble["number"],
                        "finish": position,
                    }
                )
                position -= 1

        blocked = self.client.put(
            f'/api/heats/{first_heat_id}/results', json={"results": reversed_results}
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("later heat has already started", blocked.get_json()["error"])

        still_blocked = self.client.put(
            f'/api/heats/{first_heat_id}/results',
            json={"results": reversed_results, "confirmReset": True},
        )
        self.assertEqual(still_blocked.status_code, 409)

    def test_small_tournament_auto_advances_without_racing(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Tiny Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        racers = [
            {"name": racer["name"], "color": racer["color"]}
            for racer in created["contestants"][:2]
        ]
        configured = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Tiny Cup",
                "days": 2,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 2,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 2,
                "points": [10, 7],
                "contestants": racers,
            },
        ).get_json()
        self.assertEqual(configured["competition"]["racersPerHeat"], 2)
        racer_a, racer_b = (row["id"] for row in configured["contestants"])
        state = configured
        for day in state["days"]:
            heat = day["heats"][0]
            results = [
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": entry["marbles"][0]["number"],
                    "finish": 1 if entry["contestantId"] == racer_a else 2,
                }
                for entry in heat["entries"]
            ]
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        champ = state["championship"]
        self.assertTrue(champ["wildcard"]["ready"])
        self.assertTrue(champ["wildcard"]["skipped"])
        self.assertEqual(champ["wildcard"]["fieldSize"], 0)
        self.assertTrue(champ["preliminary"]["ready"])
        self.assertTrue(champ["preliminary"]["skipped"])
        self.assertEqual(champ["preliminary"]["fieldSize"], 1)
        self.assertTrue(champ["final"]["ready"])
        final_entries = champ["final"]["heat"]["entries"]
        origins = {entry["contestantId"]: entry["originStage"] for entry in final_entries}
        self.assertEqual(origins[racer_a], "bye")
        self.assertEqual(origins[racer_b], "stage-skip")

    def test_final_field_trims_to_max_final_racers(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Trim Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        contestant_ids = [racer["id"] for racer in created["contestants"]]
        state = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Trim Cup",
                "days": 4,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 3,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()

        # Each round has its own distinct 1st/2nd place pair, so four rounds
        # bank four distinct bye winners -- more than maxFinalRacers=3 allows.
        pairs = [(0, 1), (2, 3), (4, 5), (6, 7)]
        for day_index, day in enumerate(state["days"]):
            heat = day["heats"][0]
            winner_index, second_index = pairs[day_index]
            remaining = [index for index in range(8) if index not in (winner_index, second_index)]
            finish_by_index = {winner_index: 1, second_index: 2}
            for rank, racer_index in enumerate(remaining, start=3):
                finish_by_index[racer_index] = rank
            results = [
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": entry["marbles"][0]["number"],
                    "finish": finish_by_index[contestant_ids.index(entry["contestantId"])],
                }
                for entry in heat["entries"]
            ]
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        self.assertTrue(state["championship"]["wildcard"]["ready"])
        if state["championship"]["wildcard"]["heats"]:
            state = score_all_heats_sequentially(self.client, state["championship"]["wildcard"]["heats"])
        self.assertTrue(state["championship"]["preliminary"]["ready"])
        if state["championship"]["preliminary"]["heats"]:
            state = score_all_heats_sequentially(self.client, state["championship"]["preliminary"]["heats"])

        final_stage = state["championship"]["final"]
        self.assertTrue(final_stage["ready"])
        # Four rounds each bank a distinct bye winner, so the pre-trim field
        # (at least 4 byes) exceeds maxFinalRacers=3 and must be trimmed down.
        self.assertGreater(final_stage["trimmedCount"], 0)
        self.assertEqual(len(final_stage["heat"]["entries"]), 3)
        self.assertLessEqual(final_stage["byeCount"], 3)

    def test_championship_config_validation(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Config Validation Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        racers = [
            {"name": racer["name"], "color": racer["color"]}
            for racer in created["contestants"]
        ]
        base_payload = {
            "name": "Config Validation Cup",
            "days": 3,
            "heatsPerRacerPerDay": 3,
            "maxMarblesPerHeat": 6,
            "marblesPerRacer": 1,
            "points": [10, 7, 5, 3, 2, 1],
            "contestants": racers,
        }

        missing_field = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={**base_payload, "maxByeMarblesPerRacer": 1, "maxFinalRacers": 6},
        )
        self.assertEqual(missing_field.status_code, 400)

        out_of_range = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                **base_payload,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": -1,
                "maxFinalRacers": 6,
            },
        )
        self.assertEqual(out_of_range.status_code, 400)

        invalid_promotion_count = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                **base_payload,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "wildcardRacersPromotedPerHeat": 0,
                "preliminaryRacersPromotedPerHeat": 2,
                "maxFinalRacers": 6,
            },
        )
        self.assertEqual(invalid_promotion_count.status_code, 400)

        valid = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                **base_payload,
                "championshipMaxMarblesPerHeat": 4,
                "maxByeMarblesPerRacer": 2,
                "wildcardRacersPromotedPerHeat": 3,
                "preliminaryRacersPromotedPerHeat": 1,
                "maxFinalRacers": 5,
            },
        )
        self.assertEqual(valid.status_code, 200)
        state = valid.get_json()
        self.assertEqual(state["competition"]["championshipMaxMarblesPerHeat"], 4)
        self.assertEqual(state["competition"]["maxByeMarblesPerRacer"], 2)
        self.assertEqual(state["competition"]["wildcardRacersPromotedPerHeat"], 3)
        self.assertEqual(state["competition"]["preliminaryRacersPromotedPerHeat"], 1)
        self.assertEqual(state["competition"]["maxFinalRacers"], 5)

    def test_scoring_unstarted_staging_heat_is_rejected(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Unstarted Heat Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        heat = created["days"][0]["heats"][0]
        results = [
            {
                "contestantId": entry["contestantId"],
                "marbleNumber": entry["marbles"][0]["number"],
                "finish": index + 1,
            }
            for index, entry in enumerate(heat["entries"])
        ]
        rejected = self.client.put(
            f'/api/heats/{heat["id"]}/results', json={"results": results}
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertIn("Start this heat", rejected.get_json()["error"])

        started = self.client.put(f'/api/heats/{heat["id"]}/start')
        self.assertEqual(started.status_code, 200)
        accepted = self.client.put(
            f'/api/heats/{heat["id"]}/results', json={"results": results}
        )
        self.assertEqual(accepted.status_code, 200)

    def test_staging_heats_lock_until_earlier_rounds_complete(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Lock Order Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        heats = [heat for day in created["days"] for heat in day["heats"]]
        heat1, heat2 = heats[0], heats[1]
        self.assertFalse(heat1["locked"])
        self.assertFalse(heat1["started"])
        self.assertTrue(heat2["locked"])
        self.assertFalse(heat2["started"])

        blocked = self.client.put(f'/api/heats/{heat2["id"]}/start')
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("Complete the earlier rounds", blocked.get_json()["error"])

        state = score_heat_sequentially(self.client, heat1).get_json()
        heat1_after = state["days"][0]["heats"][0]
        heat2_after = state["days"][0]["heats"][1]
        self.assertTrue(heat1_after["complete"])
        self.assertFalse(heat1_after["locked"])
        self.assertFalse(heat2_after["locked"])
        self.assertFalse(heat2_after["started"])

        unlocked_start = self.client.put(f'/api/heats/{heat2["id"]}/start')
        self.assertEqual(unlocked_start.status_code, 200)

    def test_starting_a_heat_locks_the_previous_heat_from_edits(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Edit Lock Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        heats = [heat for day in created["days"] for heat in day["heats"]]
        heat1, heat2 = heats[0], heats[1]

        state = score_heat_sequentially(self.client, heat1).get_json()
        heat1_after = state["days"][0]["heats"][0]
        self.assertFalse(heat1_after["editLocked"])

        rescored_before_next_start = self.client.put(
            f'/api/heats/{heat1["id"]}/results',
            json={"results": build_sequential_results(heat1_after)},
        )
        self.assertEqual(rescored_before_next_start.status_code, 200)

        started = self.client.put(f'/api/heats/{heat2["id"]}/start')
        self.assertEqual(started.status_code, 200)
        state_after_start = started.get_json()
        heat1_locked = state_after_start["days"][0]["heats"][0]
        self.assertTrue(heat1_locked["editLocked"])

        blocked = self.client.put(
            f'/api/heats/{heat1["id"]}/results',
            json={"results": build_sequential_results(heat1_locked)},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("later heat has already started", blocked.get_json()["error"])

    def test_championship_heats_require_start_before_scoring(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Bracket Sequencing Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        staging_heats = [heat for day in created["days"] for heat in day["heats"]]
        state = score_all_heats_sequentially(self.client, staging_heats)
        self.assertTrue(state["championship"]["wildcard"]["ready"])
        wildcard_heats = state["championship"]["wildcard"]["heats"]
        self.assertTrue(wildcard_heats)

        heat = wildcard_heats[0]
        self.assertFalse(heat["started"])
        self.assertFalse(heat["locked"])
        results = build_sequential_results(heat)

        rejected = self.client.put(
            f'/api/heats/{heat["id"]}/results', json={"results": results}
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertIn("Start this heat", rejected.get_json()["error"])

        started = self.client.put(f'/api/heats/{heat["id"]}/start')
        self.assertEqual(started.status_code, 200)
        accepted = self.client.put(
            f'/api/heats/{heat["id"]}/results', json={"results": results}
        )
        self.assertEqual(accepted.status_code, 200)

    def test_starting_the_next_stage_locks_the_previous_stage_from_edits(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Cross Stage Lock Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        staging_heats = [heat for day in created["days"] for heat in day["heats"]]
        state = score_all_heats_sequentially(self.client, staging_heats)
        self.assertTrue(state["championship"]["wildcard"]["ready"])
        wildcard_heats = state["championship"]["wildcard"]["heats"]
        self.assertTrue(wildcard_heats)
        state = score_all_heats_sequentially(self.client, wildcard_heats)
        self.assertTrue(state["championship"]["wildcard"]["complete"])
        self.assertTrue(state["championship"]["preliminary"]["ready"])
        preliminary_heats = state["championship"]["preliminary"]["heats"]
        self.assertTrue(preliminary_heats)

        last_wildcard_heat = state["championship"]["wildcard"]["heats"][-1]
        self.assertFalse(last_wildcard_heat["editLocked"])

        started = self.client.put(f'/api/heats/{preliminary_heats[0]["id"]}/start')
        self.assertEqual(started.status_code, 200)
        state_after_start = started.get_json()
        locked_wildcard_heat = state_after_start["championship"]["wildcard"]["heats"][-1]
        self.assertTrue(locked_wildcard_heat["editLocked"])

        blocked = self.client.put(
            f'/api/heats/{last_wildcard_heat["id"]}/results',
            json={"results": build_sequential_results(last_wildcard_heat)},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("later heat has already started", blocked.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
